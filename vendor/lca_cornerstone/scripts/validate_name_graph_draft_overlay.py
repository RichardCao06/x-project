#!/usr/bin/env python3
"""Verify that a production name-graph HTML exactly reflects its draft overlay inputs."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from build_name_graph_html import build, draft_preview_nodes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path)
    parser.add_argument("html", type=Path)
    parser.add_argument("--template", default="scripts/templates/name-graph.tpl.html")
    args = parser.parse_args()
    if not args.html.is_file():
        raise SystemExit(f"缺少 production name-graph HTML: {args.html}")

    slug = args.graph.name.removesuffix("-name-graph.json")
    with tempfile.TemporaryDirectory() as tmp:
        expected = Path(tmp) / args.html.name
        build(str(args.graph), str(expected), args.template, preview=False)
        matches = expected.read_bytes() == args.html.read_bytes()
    report = {
        "protocol": "wiki-draft-overlay-check-v1",
        "industry": slug,
        "nodes": draft_preview_nodes(slug),
        "scope": "read-only comparison of production name-graph HTML; Wiki bundles are not inputs",
        "html_matches_current_graph_template_overlay": matches,
        "go": matches,
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
