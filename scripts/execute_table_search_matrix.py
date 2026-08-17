#!/usr/bin/env python3
"""Execute an immutable table Search plan with cache, concurrency and checkpoints."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from urllib.parse import urlsplit

from lca_project.kernel.search_execution import SearchExecutionRuntime
from lca_project.kernel.state import StateStore


MAX_RESULTS = 5
DEFAULT_EXCERPT_CHARS = 40_000
DOCUMENT_ROUTE_EXCERPT_CHARS=250_000


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def classify(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith((".gov.cn", ".gov")): return "government_or_regulator"
    if host.endswith(("ipc.org", "iso.org", "iec.ch")): return "standard_or_industry_body"
    if host.endswith((".edu", ".edu.cn", "doi.org")): return "peer_reviewed_research"
    return "manufacturer_or_other_technical"


def execution_key(row: dict) -> str:
    """Identify one external request independently of its field fan-out."""
    request = {
        "query_hash": row.get("query_hash"),
        "query": row.get("query"),
        "language": row.get("language"),
        "document_route": row.get("document_route"),
        "document_type": row.get("document_type"),
        "seed_candidates": row.get("seed_candidates") or [],
    }
    return hashlib.sha256(json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def deduplicate_execution_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Execute each request once while retaining a key for every logical field row."""
    unique: dict[str, dict] = {}
    keys: list[str] = []
    for row in rows:
        key = execution_key(row)
        keys.append(key)
        unique.setdefault(key, {**row, "query_id": key})
    return list(unique.values()), keys


def expand_execution_results(
    rows: list[dict], keys: list[str], results: list[dict],
) -> list[dict]:
    """Fan one frozen request result back out to each table/field target."""
    by_key = {str(result["query_id"]): result for result in results}
    expanded: list[dict] = []
    for row, key in zip(rows, keys, strict=True):
        result = dict(by_key[key])
        result.pop("query_id", None)
        # Field-scoped metadata belongs to the logical plan row; request outcome
        # and frozen results belong to the unique external execution.
        expanded.append({**result, **row,
                         "status": result.get("status", row.get("status")),
                         "results": result.get("results", []),
                         "provider_attempts": result.get("provider_attempts", [])})
    return expanded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix", type=Path)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "config/search-providers.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--document-route", action="append", default=[])
    args = parser.parse_args()
    matrix = load(args.matrix)
    project_root = Path(__file__).resolve().parents[1]
    discovery_path = project_root / "vendor/lca_cornerstone/scripts/wiki_source_discovery.py"
    spec = importlib.util.spec_from_file_location("wiki_source_discovery", discovery_path)
    discovery = importlib.util.module_from_spec(spec); spec.loader.exec_module(discovery)  # type: ignore
    provider_path = project_root / "scripts/search_provider_runtime.py"
    provider_spec = importlib.util.spec_from_file_location("search_provider_runtime", provider_path)
    provider = importlib.util.module_from_spec(provider_spec); provider_spec.loader.exec_module(provider)  # type: ignore
    config = load(args.config)
    secrets = provider.load_secrets(
        args.config.resolve().parents[1] / str(config.get("secret_file", ".env.search.local"))
    )
    providers = config.get("providers") or {}; routing = config.get("routing") or {}
    policy = config.get("query_policy") or {}
    config_hash = hashlib.sha256(args.config.read_bytes()).hexdigest()
    routing_hash = hashlib.sha256(json.dumps(routing, sort_keys=True).encode()).hexdigest()
    cache_root = project_root / "var"
    execution_dir = args.matrix.parent / "search-execution"
    runtime = SearchExecutionRuntime(
        cache_root, StateStore(cache_root / "state.db"),
        query_ttl_seconds=int(policy.get("query_cache_ttl_seconds", 86400)),
        fetch_ttl_seconds=int(policy.get("fetch_cache_ttl_seconds", 604800)),
        global_limit=int(policy.get("global_concurrency", 6)),
        provider_limit=int(policy.get("provider_concurrency", 2)),
    )
    selected_routes = set(args.document_route)

    def execute(row: dict) -> dict:
        if selected_routes and row.get("document_route") not in selected_routes:
            return {**row, "status": "not_selected", "results": []}
        results: list[dict] = []; attempts: list[dict] = []
        for seed in row.get("seed_candidates", []):
            try: seed_url = discovery.validate_external_url(str(seed.get("url") or ""), [])
            except ValueError: continue
            if seed_url not in {item["url"] for item in results}:
                results.append({"url": seed_url, "title": str(seed.get("title") or ""),
                    "provider": "curated_document_route", "snippet": "",
                    "source_class": str(seed.get("source_class") or classify(seed_url))})
        language = str(row.get("language") or "zh")
        for provider_name in (routing.get(language) or routing.get("technical") or [])[:3]:
            provider_config = providers.get(provider_name) or {}
            if not provider_config.get("enabled", False): continue
            def search_call():
                hits, status = provider.provider_search(
                    provider_name, provider_config, row["query"], locator=row["field"],
                    secrets=secrets, timeout=int(min(max(args.timeout, 1), 15)), limit=MAX_RESULTS,
                )
                return {"hits": hits, "status": status}
            try:
                searched, cache_hit, cache_key = runtime.search(
                    query=row["query"], language=language, provider_id=provider_name,
                    provider_config_version=config_hash, routing_policy_version=routing_hash,
                    operation=search_call,
                )
                hits = searched["hits"]
                attempts.append({"provider": provider_name, "status": searched["status"],
                                 "results": len(hits), "cache_hit": cache_hit,
                                 "cache_key": cache_key})
                for hit in hits:
                    if hit.get("url") not in {item.get("url") for item in results}:
                        results.append(hit)
                if hits: break
            except Exception as exc:
                attempts.append({"provider": provider_name, "status": "provider_error",
                                 "results": 0, "error": {"code": type(exc).__name__,
                                 "message": str(exc)}})
        frozen: list[dict] = []
        for candidate in results[:MAX_RESULTS]:
            try: url = discovery.validate_external_url(candidate.get("url", ""), [])
            except ValueError: continue
            frozen_row = {"url": url, "title": candidate.get("title", ""),
                          "provider": candidate.get("provider", ""),
                          "snippet": candidate.get("snippet", ""),
                          "source_class": candidate.get("source_class") or classify(url),
                          "current_job_status": "candidate_unverified",
                          "document_route": row.get("document_route"),
                          "document_type": row.get("document_type")}
            def fetch_call():
                payload, final_url, content_type, redirects = discovery.safe_http_get(
                    url, timeout=min(max(args.timeout, 1), 15), max_bytes=12_000_000,
                    allowlist=[], max_redirects=5,
                )
                locator = str(row.get("query") or "") if row.get("document_route") else ""
                excerpt_chars = (DOCUMENT_ROUTE_EXCERPT_CHARS if row.get("document_route")
                                 else DEFAULT_EXCERPT_CHARS)
                media, excerpt = discovery.extract_excerpt(
                    final_url, payload, content_type, excerpt_chars, locator
                )
                return payload, {"url": final_url, "content_type": media,
                                 "excerpt": excerpt, "redirects": redirects}
            try:
                payload, metadata, cache_hit, cache_key = runtime.fetch(
                    url=url, fetch_policy_version="wiki-fetch-v1",
                    accepted_media_types=["text/html", "application/pdf"],
                    extractor_version="wiki-source-discovery-v1", operation=fetch_call,
                )
                payload_dir = args.matrix.parent / "search-payloads"; payload_dir.mkdir(exist_ok=True)
                payload_path = payload_dir / f"{row['query_hash']}-{len(frozen)+1}.payload"
                if not payload_path.exists(): payload_path.write_bytes(payload)
                frozen_row.update(metadata); frozen_row.update({
                    "fetch_status": "fetched" if metadata.get("excerpt") else "empty",
                    "payload_path": str(payload_path.resolve()), "fetch_cache_hit": cache_hit,
                    "fetch_cache_key": cache_key,
                })
            except Exception as exc:
                frozen_row.update({"fetch_status": "error", "error": {
                    "code": type(exc).__name__, "message": str(exc)}})
            frozen.append(frozen_row)
        status = ("fetched" if any(item.get("fetch_status") == "fetched" for item in frozen)
                  else "found" if frozen else "not_found")
        return {**row, "status": status, "results": frozen,
                "provider_attempts": attempts}

    plan_rows = list(matrix.get("queries", []))
    execution_rows, execution_keys = deduplicate_execution_rows(plan_rows)
    report = runtime.execute(execution_rows, execution_dir=execution_dir, execute_one=execute,
                             max_workers=int(policy.get("global_concurrency", 6)))
    expanded_results = expand_execution_results(
        plan_rows, execution_keys, list(report.results)
    )
    output = args.output or args.matrix.with_name("search-matrix.executed.json")
    executed = {**matrix, "protocol": "wiki-table-search-executed-v2",
                "coverage_status": "executed",
                "plan_sha256": hashlib.sha256(args.matrix.read_bytes()).hexdigest(),
                "queries": expanded_results, "execution_manifest": str(report.manifest_path),
                "usage": {"search_requests": len(execution_rows),
                          "logical_queries": len(plan_rows),
                          "unique_queries": len(execution_rows),
                          "deduplicated_queries": len(plan_rows) - len(execution_rows),
                          "query_cache_hits": report.query_cache_hits,
                          "query_cache_misses": report.query_cache_misses,
                          "checkpoint_hits": report.checkpoint_hits}}
    output.write_text(json.dumps(executed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps({"output": str(output), "queries": len(plan_rows),
                      "unique_queries": len(execution_rows),
                      "deduplicated_queries": len(plan_rows) - len(execution_rows),
                      "cache_hits": report.query_cache_hits,
                      "checkpoint_hits": report.checkpoint_hits}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
