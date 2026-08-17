"""Content-addressed Search/Fetch caches and SQLite-backed global concurrency slots."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from .state import StateStore, utcnow


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def normalize_query(value: str) -> str:
    return " ".join(value.casefold().split())


def canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host + (f":{port}" if port and port not in {80, 443} else "")
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def query_cache_key(*, query: str, language: str, provider_id: str,
                    provider_config_version: str, routing_policy_version: str) -> str:
    return hashlib.sha256(canonical_json({
        "normalized_query": normalize_query(query), "language": language,
        "provider_id": provider_id, "provider_config_version": provider_config_version,
        "routing_policy_version": routing_policy_version,
    })).hexdigest()


def fetch_cache_key(*, url: str, fetch_policy_version: str,
                    accepted_media_types: list[str], extractor_version: str) -> str:
    return hashlib.sha256(canonical_json({
        "canonical_url": canonical_url(url), "fetch_policy_version": fetch_policy_version,
        "accepted_media_types": sorted(accepted_media_types),
        "extractor_version": extractor_version,
    })).hexdigest()


@dataclass(frozen=True)
class CacheHit:
    key: str
    value: dict[str, Any]


class JsonCache:
    def __init__(self, root: str | Path, *, ttl_seconds: int) -> None:
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / key / "entry.json"

    def get(self, key: str) -> CacheHit | None:
        path = self._path(key)
        if not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(str(document["created_at"]))
        if datetime.now(timezone.utc) - created > timedelta(seconds=int(document["ttl_seconds"])):
            return None
        value = document.get("value")
        if document.get("value_sha256") != hashlib.sha256(canonical_json(value)).hexdigest():
            raise RuntimeError(f"cache integrity failure: {key}")
        return CacheHit(key, value)

    def put(self, key: str, value: dict[str, Any], *, metadata: dict[str, Any]) -> CacheHit:
        path = self._path(key); path.parent.mkdir(parents=True, exist_ok=True)
        document = {"protocol": "wiki-cache-entry-v1", "key": key, "created_at": utcnow(),
                    "ttl_seconds": self.ttl_seconds,
                    "value_sha256": hashlib.sha256(canonical_json(value)).hexdigest(),
                    "metadata": metadata, "value": value}
        temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_bytes(canonical_json(document))
        os.replace(temporary, path)
        return CacheHit(key, value)


class GlobalRateLimiter:
    """Database slots enforce limits across workers, not only within one process."""

    def __init__(self, state: StateStore) -> None:
        self.state = state
        with self.state.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS search_rate_slots(
              scope TEXT NOT NULL, slot INTEGER NOT NULL, holder TEXT NOT NULL,
              expires_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(scope,slot))""")

    @contextmanager
    def slot(self, scope: str, holder: str, limit: int, *, ttl_seconds: int = 60,
             wait_seconds: float = 25.0) -> Iterator[None]:
        if limit <= 0:
            raise ValueError("rate limit must be positive")
        deadline = time.monotonic() + wait_seconds
        claimed: int | None = None
        while claimed is None:
            now = utcnow()
            expiry = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
            with self.state.transaction() as conn:
                conn.execute("DELETE FROM search_rate_slots WHERE scope=? AND expires_at<=?",
                             (scope, now))
                occupied = {int(row["slot"]) for row in conn.execute(
                    "SELECT slot FROM search_rate_slots WHERE scope=?", (scope,)
                )}
                for candidate in range(limit):
                    if candidate not in occupied:
                        conn.execute("INSERT INTO search_rate_slots VALUES(?,?,?,?,?)",
                                     (scope, candidate, holder, expiry, now))
                        claimed = candidate; break
            if claimed is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"global rate slot timeout: {scope}")
                time.sleep(0.05)
        try:
            yield
        finally:
            with self.state.transaction() as conn:
                conn.execute("DELETE FROM search_rate_slots WHERE scope=? AND slot=? AND holder=?",
                             (scope, claimed, holder))
