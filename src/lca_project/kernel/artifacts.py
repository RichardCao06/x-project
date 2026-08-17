"""Content-addressed immutable artifacts with provenance edges."""
from __future__ import annotations

import hashlib
import json
import os
import mimetypes
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

    def put_file(self, path: str | Path, *, metadata: dict[str, Any] | None = None) -> StoredArtifact:
        """Freeze one real file into CAS; the source path is never authoritative."""
        source = Path(path).resolve()
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"artifact source must be a real file: {source}")
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        return self.put_bytes(source.read_bytes(), media_type=media_type, metadata=metadata)

    def put_task_output_manifest(
        self,
        workspace: str | Path,
        outputs: list[dict[str, Any]],
        execution_result: dict[str, Any],
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        input_hashes: tuple[str, ...] = (),
        lineage_files: dict[str, str | Path] | None = None,
    ) -> StoredArtifact:
        """Freeze task files and its execution result under one immutable manifest."""
        root = Path(workspace).resolve()
        if not root.is_dir():
            raise ValueError(f"task workspace does not exist: {root}")
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in outputs:
            logical = str(item.get("path") or "").strip()
            if not logical or logical in seen:
                raise ValueError(f"invalid or duplicate task output path: {logical!r}")
            source = (root / logical).resolve()
            if not source.is_relative_to(root):
                raise ValueError(f"task output escapes workspace: {logical}")
            frozen = self.put_file(source, metadata={
                "schema": "task-output-file-v1", "run_id": run_id,
                "task_id": task_id, "attempt_id": attempt_id, "logical_path": logical,
            })
            entries.append({
                "path": logical, "sha256": frozen.digest, "size": frozen.size,
                "media_type": frozen.media_type,
                "role": str(item.get("role") or "protocol_artifact"),
            })
            seen.add(logical)
        if not entries:
            raise ValueError("task output manifest requires at least one file")
        result = self.put_json(execution_result, metadata={
            "schema": "capability-execution-result-v1", "run_id": run_id,
            "task_id": task_id, "attempt_id": attempt_id,
        })
        lineage: list[dict[str, Any]] = []
        for relation, source in sorted((lineage_files or {}).items()):
            frozen = self.put_file(source, metadata={
                "schema": "task-lineage-input-v1", "relation": relation,
                "run_id": run_id, "task_id": task_id, "attempt_id": attempt_id,
            })
            lineage.append({
                "relation": relation, "sha256": frozen.digest, "size": frozen.size,
                "media_type": frozen.media_type,
            })
        for digest in input_hashes:
            self.get_bytes(digest)
            lineage.append({"relation": "task_input", "sha256": digest})
        manifest_value = {
            "protocol": "task-output-manifest-v1", "run_id": run_id,
            "task_id": task_id, "attempt_id": attempt_id,
            "execution_result_hash": result.digest,
            "files": sorted(entries, key=lambda row: row["path"]),
            "lineage": sorted(lineage, key=lambda row: (row["relation"], row["sha256"])),
        }
        manifest = self.put_json(manifest_value, metadata={
            "schema": "task-output-manifest-v1", "run_id": run_id,
            "task_id": task_id, "attempt_id": attempt_id,
        })
        self.link(result.digest, manifest.digest, "task_execution_result")
        for entry in entries:
            self.link(str(entry["sha256"]), manifest.digest, "task_output_file")
        for entry in lineage:
            self.link(str(entry["sha256"]), manifest.digest, str(entry["relation"]))
        return manifest

    def verify_task_output_manifest(self, digest: str) -> dict[str, Any]:
        """Verify a task manifest and every referenced CAS object without a workspace."""
        document = json.loads(self.get_bytes(digest))
        if document.get("protocol") != "task-output-manifest-v1":
            raise ValueError("artifact is not a task-output-manifest-v1")
        result_hash = str(document.get("execution_result_hash") or "")
        self.get_bytes(result_hash)
        for item in document.get("files") or []:
            content = self.get_bytes(str(item.get("sha256") or ""))
            if len(content) != int(item.get("size", -1)):
                raise RuntimeError(f"task output size mismatch: {item.get('path')}")
        for item in document.get("lineage") or []:
            content = self.get_bytes(str(item.get("sha256") or ""))
            if "size" in item and len(content) != int(item["size"]):
                raise RuntimeError(f"task lineage size mismatch: {item.get('relation')}")
        return document

    def verify_materialized_outputs(
        self, workspace: str | Path, digest: str
    ) -> list[dict[str, Any]]:
        """Verify task-owned physical state against its frozen CAS manifest."""
        root = Path(workspace).resolve()
        document = self.verify_task_output_manifest(digest)
        verified: list[dict[str, Any]] = []
        for item in document.get("files") or []:
            if item.get("role") != "materialized_output":
                continue
            logical = str(item.get("path") or "")
            target = (root / logical).resolve()
            if not target.is_relative_to(root) or not target.is_file() or target.is_symlink():
                raise RuntimeError(f"materialized output is missing or unsafe: {logical}")
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != item.get("sha256"):
                raise RuntimeError(f"materialized output drift: {logical}")
            verified.append(dict(item))
        return verified

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
