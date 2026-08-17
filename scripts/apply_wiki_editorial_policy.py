#!/usr/bin/env python3
"""Apply the risk-tier editorial policy to one frozen review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from lca_project.domains.editorial_policy import apply_editorial_policy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", type=Path)
    parser.add_argument("content", type=Path)
    parser.add_argument("publication_mode", choices=("preview", "reviewed"))
    parser.add_argument("--usage", type=Path)
    args = parser.parse_args()
    result = apply_editorial_policy(args.review.resolve(), args.content.resolve(),
                                    args.publication_mode, usage_path=args.usage.resolve()
                                    if args.usage else None)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["decision"] != "block" else 2


if __name__ == "__main__":
    raise SystemExit(main())
