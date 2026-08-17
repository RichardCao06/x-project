#!/usr/bin/env python3
"""Apply a generic hash-bound Wiki paragraph repair and write its receipt."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from lca_project.domains.editorial_patch import apply_repairs


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft", type=Path); parser.add_argument("blueprint", type=Path)
    parser.add_argument("review", type=Path); parser.add_argument("repairs", type=Path)
    parser.add_argument("output", type=Path); parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    repair_doc = load(args.repairs)
    repairs = repair_doc.get("repairs") if isinstance(repair_doc, dict) else repair_doc
    result, receipt = apply_repairs(
        load(args.draft), load(args.blueprint), load(args.review), repairs
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
