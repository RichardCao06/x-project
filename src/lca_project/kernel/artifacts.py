"""Content-addressed immutable artifacts with provenance edges."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import StateStore, utcnow


@dataclass(frozen=True)
class StoredArtifact:
    digest: str
    media_type: str
    size: int
    uri: str
    metadata: dict[str, Any]


class ArtifactStore:
    def __init__(self, root: str | Path, state: StateStore) -> None:
        self.root = Path(root)
        self.state = state
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        return self.root / "sha256" / digest[:2] / digest[2:4] / digest

    def put_bytes(self, content: bytes, *, media_type: str = "application/octet-stream", metadata: dict[str, Any] | None = None) -> StoredArtifact:
        digest = hashlib.sha256(content).hexdigest()
        path = self._path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != content:
            raise RuntimeError(f"CAS collision at {path}")
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            with open(temporary, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        record = StoredArtifact(digest, media_type, len(content), str(path), metadata or {})
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT INTO artifacts(digest,media_type,size,uri,metadata,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(digest) DO NOTHING",
                (record.digest, record.media_type, record.size, record.uri, json.dumps(record.metadata, sort_keys=True), utcnow()),
            )
        return record

    def put_json(self, value: Any, *, metadata: dict[str, Any] | None = None) -> StoredArtifact:
        return self.put_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(), media_type="application/json", metadata=metadata)

    def get_bytes(self, digest: str) -> bytes:
        record = self.state.get("artifacts", digest)
        if record is None:
            raise KeyError(digest)
        content = Path(record["uri"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            metadata = dict(record.get("metadata") or {})
            metadata["integrity_status"] = "corrupt"
            with self.state.transaction() as conn:
                conn.execute("UPDATE artifacts SET metadata=? WHERE digest=?", (json.dumps(metadata, sort_keys=True), digest))
            raise RuntimeError(f"artifact integrity failure: {digest}")
        return content

    def link(self, parent_digest: str, child_digest: str, relation: str = "derived_from") -> None:
        if not relation or len(relation) > 120:
            raise ValueError("invalid relation")
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO artifact_edges VALUES(?,?,?,?) ON CONFLICT DO NOTHING", (parent_digest, child_digest, relation, utcnow()))

    def lineage(self, digest: str) -> list[dict[str, str]]:
        return [dict(row) for row in self.state._connection().execute("SELECT parent_digest,child_digest,relation,created_at FROM artifact_edges WHERE child_digest=?", (digest,))]
