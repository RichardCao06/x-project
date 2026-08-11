#!/usr/bin/env python3
"""Freeze externally supplied search hits against an exact Wiki query queue.

This is the deterministic boundary between a search provider and the Wiki
fetcher.  It refuses missing/extra query IDs and copies the queue's exact
query/hash so a human or agent cannot silently rewrite the research target.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit


def parse_hit(value: str) -> tuple[str, dict[str, str]]:
    parts = value.split("|", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("hit must be QUERY_ID|URL|TITLE")
    query_id, url, title = parts
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise argparse.ArgumentTypeError("hit URL must be public http(s)")
    return query_id, {"url": url, "title": title}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queue", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--hit", action="append", default=[], type=parse_hit)
    parser.add_argument("--not-found", action="append", default=[])
    parser.add_argument("--backend", default="frozen-search-provider")
    parser.add_argument("--cost-usd", type=float, default=0.0)
    args = parser.parse_args()
    queue = json.loads(args.queue.read_text(encoding="utf-8"))
    queries = queue.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("queue has no queries")
    hits = dict(args.hit)
    not_found = set(args.not_found)
    expected = {str(item["query_id"]) for item in queries}
    supplied = set(hits) | not_found
    if supplied != expected or set(hits) & not_found:
        raise ValueError(f"query coverage mismatch: missing={sorted(expected-supplied)} extra={sorted(supplied-expected)}")
    rows = []
    for item in queries:
        query_id = str(item["query_id"])
        result = hits.get(query_id)
        rows.append({
            "search_hash": item["search_hash"], "query": item["query"],
            "status": "found" if result else "not_found",
            "results": [result] if result else [],
        })
    document = {
        "protocol": {"version": "wiki-frozen-search-v1", "kind": "query-search-results"},
        "backend": args.backend,
        "usage": {"search_requests": len(queries), "cost_usd": args.cost_usd},
        "queries": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "queries": len(rows), "found": len(hits)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
