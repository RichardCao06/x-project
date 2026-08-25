#!/usr/bin/env python3
"""Execute a frozen Wiki Source Queue through configured search providers.

Provider attempts are audit data; successful URLs remain candidates only.  The
deterministic Wiki fetcher still re-fetches, hashes and localizes every result.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_secrets(path: Path) -> dict[str, str]:
    values = dict(os.environ)
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def request_json(url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: int) -> tuple[dict[str, Any], bytes]:
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
        raw = response.read()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("provider response is not an object")
    return value, raw


def normalize_hits(rows: Any, *, provider: str, locator: str, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("sourceURL") or "").strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or url in seen:
            continue
        seen.add(url)
        results.append({
            "url": url, "title": str(row.get("title") or ""),
            "snippet": str(row.get("snippet") or row.get("description") or row.get("text") or "")[:2000],
            "provider": provider, "locator": locator,
        })
        if len(results) >= limit:
            break
    return results


def provider_search(name: str, spec: dict[str, Any], query: str, *, locator: str,
                    secrets: dict[str, str], timeout: int, limit: int) -> tuple[list[dict[str, str]], str]:
    auth = spec.get("auth") or {}
    env_name = str(auth.get("api_key_env") or "")
    key = secrets.get(env_name, "") if env_name else ""
    if env_name and not key:
        return [], "not_configured"
    defaults = dict(spec.get("request_defaults") or {})
    headers: dict[str, str] = {}
    if name == "baidu_qianfan":
        headers[str(auth.get("header") or "X-Appbuilder-Authorization")] = f"Bearer {key}"
        body = {**defaults, "messages": [{"role": "user", "content": query}]}
        data, _ = request_json(str(spec["endpoint"]), headers=headers, body=body, timeout=timeout)
        return normalize_hits(data.get("references"), provider=name, locator=locator, limit=limit), "ok"
    if name == "exa":
        headers[str(auth.get("header") or "x-api-key")] = key
        body = {**defaults, "query": query, "numResults": limit}
        data, _ = request_json(str(spec["endpoint"]), headers=headers, body=body, timeout=timeout)
        return normalize_hits(data.get("results"), provider=name, locator=locator, limit=limit), "ok"
    if name == "firecrawl":
        headers[str(auth.get("header") or "Authorization")] = f"Bearer {key}"
        body = {**defaults, "query": query, "limit": limit}
        data, _ = request_json(str(spec["endpoint"]), headers=headers, body=body, timeout=timeout)
        return normalize_hits((data.get("data") or {}).get("web"), provider=name, locator=locator, limit=limit), "ok"
    # Browser handoff and HTML scraping are deliberately not executed by this
    # API adapter.  Their attempt remains visible rather than becoming false
    # not_found evidence.
    return [], "handoff_required" if name == "google_browser" else "backend_blocked"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("queue", type=Path); ap.add_argument("config", type=Path); ap.add_argument("output", type=Path)
    ap.add_argument("--research-scout", type=Path, required=True)
    ap.add_argument("--research-scout-sha256", required=True)
    args = ap.parse_args()
    queue, config = load(args.queue), load(args.config)
    advisory_candidates: list[dict[str, Any]] = []
    supplied_scout_path = args.research_scout.resolve()
    if not supplied_scout_path.is_file():
        raise ValueError("supplied research scout is missing")
    supplied_scout_sha256 = hashlib.sha256(supplied_scout_path.read_bytes()).hexdigest()
    if supplied_scout_sha256 != args.research_scout_sha256:
        raise ValueError("supplied research scout SHA-256 mismatch")
    queue_scout_record = queue.get("research_scout")
    expected_scout_record = {
        "path": str(supplied_scout_path), "sha256": supplied_scout_sha256,
    }
    if queue_scout_record != expected_scout_record:
        raise ValueError("source queue is not bound to the supplied research scout")
    scout = load(supplied_scout_path)
    if (scout.get("protocol") != "wiki-research-scout-v1"
            or not isinstance(scout.get("candidates"), list)):
        raise ValueError("supplied research scout is invalid")
    repair = scout.get("diversity_repair") or {}
    excluded_urls = {
        str(url).strip() for url in repair.get("excluded_urls") or [] if str(url).strip()
    }
    excluded_url_hashes = {
        str(value).strip() for value in repair.get("excluded_url_hashes") or []
        if str(value).strip()
    }
    if any(hashlib.sha256(url.encode("utf-8")).hexdigest() not in excluded_url_hashes
           for url in excluded_urls):
        raise ValueError("research scout failed-URL hash binding is invalid")
    scout_candidates = [
        row for row in scout.get("candidates", [])
        if isinstance(row, dict) and row.get("url")
        and str(row.get("url")).strip() not in excluded_urls
        and hashlib.sha256(str(row.get("url")).strip().encode("utf-8")).hexdigest()
        not in excluded_url_hashes
    ]
    plan_record = queue.get("research_plan")
    if not isinstance(plan_record, dict) or not plan_record.get("path"):
        raise ValueError("source queue has no hash-bound research plan")
    plan_path = Path(str(plan_record["path"])).resolve()
    if (not plan_path.is_file()
            or hashlib.sha256(plan_path.read_bytes()).hexdigest() != plan_record.get("sha256")):
        raise ValueError("source queue research plan SHA-256 mismatch")
    plan = load(plan_path)
    if scout.get("node_id") != plan.get("node_id"):
        raise ValueError("supplied research scout does not match the research plan")
    advisory_candidates = [row for row in plan.get("advisory_candidates", [])
                           if isinstance(row, dict) and row.get("url")
                           and str(row.get("url")).strip() not in excluded_urls
                           and hashlib.sha256(str(row.get("url")).strip().encode("utf-8")).hexdigest()
                           not in excluded_url_hashes]
    secret_path = args.config.resolve().parents[1] / str(config.get("secret_file", ".env.search.local"))
    secrets = load_secrets(secret_path)
    policy = config.get("query_policy") or {}; timeout = int(policy.get("timeout_seconds", 30))
    limit = int(policy.get("max_results_per_query", 10)); providers = config.get("providers") or {}
    routing = config.get("routing") or {}; attempts: list[dict[str, Any]] = []; rows = []
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for item in queue.get("queries", []):
        by_hash.setdefault(str(item["search_hash"]), []).append(item)
    total_requests = 0
    for search_hash, items in by_hash.items():
        source_query = str(items[0]["query"]); tracks = [track for item in items for track in item.get("research_tracks", [])]
        if not tracks:
            tracks = [{"language": "zh", "query": source_query, "terms": []}]
        found: list[dict[str, str]] = []; seen: set[str] = set()
        believed_sources = {str((item.get("claim") or {}).get("believed_source", "")) for item in items}
        for candidate in [*scout_candidates, *advisory_candidates]:
            title = str(candidate.get("title", "")).strip()
            url = str(candidate.get("url", "")).strip()
            if any(title and (title in source or source in title) for source in believed_sources) and url not in seen:
                candidate_provider = "research_scout" if candidate in scout_candidates else "research_plan_advisory"
                seen.add(url); found.append({"url": url, "title": title, "snippet": str(candidate.get("snippet", "")),
                                             "provider": candidate_provider, "locator": ""})
                attempts.append({"search_hash": search_hash, "provider": candidate_provider,
                                 "language": "source", "query": source_query, "status": "candidate",
                                 "results": 1, "error": None,
                                 "requested_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        # A source-specific claim must be independently checked against the
        # nominated source. Search hits from another publisher are useful for
        # a new claim, but may not silently substitute for this source binding.
        source_bound = bool(found)
        for track in ([] if source_bound else tracks):
            language = str(track.get("language") or "zh"); query = str(track.get("query") or source_query)
            locator = " | ".join(str(x) for x in track.get("terms", []) if str(x).strip())
            route = routing.get(language) or routing.get("technical") or []
            for provider_name in route:
                spec = providers.get(provider_name) or {}
                if not spec.get("enabled", False):
                    continue
                started = dt.datetime.now(dt.timezone.utc)
                try:
                    hits, status = provider_search(provider_name, spec, query, locator=locator,
                                                   secrets=secrets, timeout=timeout, limit=limit)
                    error = None
                except urllib.error.HTTPError as exc:
                    hits, status, error = [], "provider_error", f"HTTP {exc.code}"
                except Exception as exc:
                    hits, status, error = [], "provider_error", f"{type(exc).__name__}: {exc}"
                total_requests += int(status not in {"not_configured", "handoff_required"})
                attempts.append({"search_hash": search_hash, "provider": provider_name, "language": language,
                                 "query": query, "status": "found" if hits else status,
                                 "results": len(hits), "error": error,
                                 "requested_at": started.isoformat()})
                accepted_hit = False
                for hit in hits:
                    hit_url = str(hit["url"]).strip()
                    hit_hash = hashlib.sha256(hit_url.encode("utf-8")).hexdigest()
                    if (hit_url not in seen and hit_url not in excluded_urls
                            and hit_hash not in excluded_url_hashes):
                        seen.add(hit["url"]); found.append(hit); accepted_hit = True
                if accepted_hit:
                    break
        rows.append({"search_hash": search_hash, "query": source_query,
                     "status": "found" if found else "not_found", "results": found[:limit]})
    payload = {"protocol": {"version": "wiki-frozen-search-v1", "kind": "query-search-results"},
               "backend": "configured-multi-provider-v1", "usage": {"search_requests": len(rows),
               "provider_requests": total_requests, "cost_usd": 0.0}, "queries": rows,
               "provider_attempts": attempts,
               "research_scout": expected_scout_record,
               "excluded_url_hashes": sorted(excluded_url_hashes),
               "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "queries": len(rows),
                      "found": sum(row["status"] == "found" for row in rows),
                      "provider_requests": total_requests}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
