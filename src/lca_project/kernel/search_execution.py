"""Concurrent, checkpointed Search/Fetch runtime shared by Wiki executors."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable
import uuid

from .search_cache import (
    GlobalRateLimiter, JsonCache, canonical_json, fetch_cache_key, query_cache_key,
)
from .state import StateStore, utcnow


@dataclass(frozen=True)
class SearchExecutionReport:
    results: tuple[dict[str, Any], ...]
    query_cache_hits: int
    query_cache_misses: int
    checkpoint_hits: int
    manifest_path: Path


class SearchExecutionRuntime:
    def __init__(self, root: str | Path, state: StateStore, *, query_ttl_seconds: int = 86400,
                 fetch_ttl_seconds: int = 604800, global_limit: int = 6,
                 provider_limit: int = 2) -> None:
        self.root = Path(root)
        self.query_cache = JsonCache(self.root / "search-cache", ttl_seconds=query_ttl_seconds)
        self.fetch_cache = JsonCache(self.root / "fetch-cache", ttl_seconds=fetch_ttl_seconds)
        self.rate = GlobalRateLimiter(state)
        self.global_limit, self.provider_limit = global_limit, provider_limit
        self._counter_lock = threading.Lock()
        self.query_cache_hits = self.query_cache_misses = 0

    def search(self, *, query: str, language: str, provider_id: str,
               provider_config_version: str, routing_policy_version: str,
               operation: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], bool, str]:
        key = query_cache_key(
            query=query, language=language, provider_id=provider_id,
            provider_config_version=provider_config_version,
            routing_policy_version=routing_policy_version,
        )
        hit = self.query_cache.get(key)
        if hit is not None:
            with self._counter_lock: self.query_cache_hits += 1
            return hit.value, True, key
        holder = f"search:{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex[:8]}"
        with self.rate.slot("search:global", holder, self.global_limit), self.rate.slot(
            f"search:provider:{provider_id}", holder, self.provider_limit
        ):
            # Recheck after waiting: a concurrent worker may have populated it.
            hit = self.query_cache.get(key)
            if hit is not None:
                with self._counter_lock: self.query_cache_hits += 1
                return hit.value, True, key
            value = operation()
            self.query_cache.put(key, value, metadata={
                "provider": provider_id, "language": language,
                "provider_config_version": provider_config_version,
                "routing_policy_version": routing_policy_version,
            })
            with self._counter_lock: self.query_cache_misses += 1
            return value, False, key

    def fetch(self, *, url: str, fetch_policy_version: str,
              accepted_media_types: list[str], extractor_version: str,
              operation: Callable[[], tuple[bytes, dict[str, Any]]]) -> tuple[bytes, dict[str, Any], bool, str]:
        key = fetch_cache_key(
            url=url, fetch_policy_version=fetch_policy_version,
            accepted_media_types=accepted_media_types, extractor_version=extractor_version,
        )
        hit = self.fetch_cache.get(key)
        payload_path = self.root / "fetch-cache" / key[:2] / key / "payload"
        if hit is not None and payload_path.is_file():
            payload = payload_path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != hit.value.get("content_sha256"):
                raise RuntimeError(f"fetch cache payload integrity failure: {key}")
            return payload, hit.value, True, key
        holder = f"fetch:{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex[:8]}"
        hostname = url.split("/", 3)[2].lower()
        with self.rate.slot(f"fetch:domain:{hostname}", holder, 2):
            hit = self.fetch_cache.get(key)
            if hit is not None and payload_path.is_file():
                payload = payload_path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != hit.value.get("content_sha256"):
                    raise RuntimeError(f"fetch cache payload integrity failure: {key}")
                return payload, hit.value, True, key
            payload, metadata = operation()
            value = {**metadata, "content_sha256": hashlib.sha256(payload).hexdigest(),
                     "bytes": len(payload)}
            self.fetch_cache.put(key, value, metadata={
                "url": url, "fetch_policy_version": fetch_policy_version,
                "extractor_version": extractor_version,
            })
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = payload_path.with_suffix(
                f".{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_bytes(payload); os.replace(temporary, payload_path)
            return payload, value, False, key

    def execute(self, rows: list[dict[str, Any]], *, execution_dir: str | Path,
                execute_one: Callable[[dict[str, Any]], dict[str, Any]],
                max_workers: int = 6) -> SearchExecutionReport:
        directory = Path(execution_dir); directory.mkdir(parents=True, exist_ok=True)
        checkpoints: dict[str, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = []
        checkpoint_hits = 0
        for row in rows:
            query_id = str(row.get("query_id") or row.get("query_hash") or row.get("search_hash") or "")
            if not query_id:
                raise ValueError("query requires a stable query_id/query_hash/search_hash")
            plan_hash = hashlib.sha256(canonical_json(row)).hexdigest()
            path = directory / f"{query_id}.json"
            if path.is_file():
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("plan_hash") == plan_hash and value.get("status") == "completed":
                    checkpoints[query_id] = value; checkpoint_hits += 1; continue
            pending.append({**row, "_query_id": query_id, "_plan_hash": plan_hash})

        def run(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            result = execute_one({key: value for key, value in row.items() if not key.startswith("_")})
            document = {"protocol": "wiki-query-checkpoint-v1", "query_id": row["_query_id"],
                        "plan_hash": row["_plan_hash"], "status": "completed",
                        "completed_at": utcnow(), "result": result}
            path = directory / f"{row['_query_id']}.json"
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_bytes(canonical_json(document)); os.replace(temporary, path)
            return row["_query_id"], document

        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, self.global_limit))) as pool:
            futures = [pool.submit(run, row) for row in pending]
            for future in as_completed(futures):
                query_id, document = future.result(); checkpoints[query_id] = document
        ordered = tuple(checkpoints[
            str(row.get("query_id") or row.get("query_hash") or row.get("search_hash"))
        ]["result"] for row in rows)
        entries = [{"query_id": key, "checkpoint_sha256": hashlib.sha256(
            (directory / f"{key}.json").read_bytes()).hexdigest()}
            for key in sorted(checkpoints)]
        manifest = {"protocol": "wiki-search-execution-manifest-v1", "created_at": utcnow(),
                    "queries": entries, "counts": {"total": len(rows),
                    "checkpoint_hits": checkpoint_hits,
                    "query_cache_hits": self.query_cache_hits,
                    "query_cache_misses": self.query_cache_misses}}
        manifest_path = directory.parent / "search-execution-manifest.json"
        temporary = manifest_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(canonical_json(manifest)); os.replace(temporary, manifest_path)
        return SearchExecutionReport(ordered, self.query_cache_hits, self.query_cache_misses,
                                     checkpoint_hits, manifest_path)
