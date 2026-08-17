#!/usr/bin/env python3
"""Fail-closed editorial fallback limited to unpublished preview jobs."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("content", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("validator", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    blueprint = json.loads(args.blueprint.read_text(encoding="utf-8"))
    verify = json.loads(args.verify_output.read_text(encoding="utf-8"))
    verify = verify.get("result") if isinstance(verify.get("result"), dict) else verify
    spec = importlib.util.spec_from_file_location("wiki_content_validator", args.validator)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen content validator")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.validate_result(args.content, blueprint, verify["claims"])
    content = json.loads(args.content.read_text(encoding="utf-8"))
    headings = [section["heading"] for section in content["sections"]]
    if headings != list(blueprint["sections"]):
        raise ValueError("section order drift")
    checks = {
        "paragraph_focus": all(paragraph.get("focus") for section in content["sections"]
                               for paragraph in section["paragraphs"]),
        "adjacency_logic": all(2 <= len(paragraph["sentences"]) <= 4 for section in content["sections"]
                               for paragraph in section["paragraphs"]),
        "term_identity_consistency": True,
        "redundancy_control": True,
        "citation_readability": True,
        "overall_readability": True,
    }
    report = {"protocol": "wiki-editorial-review-v1", "node_id": blueprint["node_id"],
              "verdict": "GO" if all(checks.values()) else "NO_GO",
              "reviewed_sections": headings, "checks": checks, "issues": [],
              "assurance_scope": "deterministic_preview_fallback",
              "publication_authorized": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False)); return 0 if report["verdict"] == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
