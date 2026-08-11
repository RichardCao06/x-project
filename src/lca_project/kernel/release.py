"""Hash-locked stage/apply/rollback for artifacts destined for a worktree."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from .proofs import ProofAuthority, ProofError


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class StagedRelease:
    id: str
    root: Path
    manifest: dict[str, str]
    expected_current: dict[str, str | None]
    gate_results: tuple[dict[str, object], ...] = ()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReleaseManager:
    DEFAULT_GATES = {f"G{index}" for index in range(8)}

    def __init__(self, release_root: str | Path, *, required_gates: set[str] | None = None,
                 proof_authority: ProofAuthority | None = None) -> None:
        self.release_root = Path(release_root)
        # Fail closed for production.  An explicit empty set is reserved for
        # isolated preview/tests and remains visible at the call site.
        self.required_gates = self.DEFAULT_GATES if required_gates is None else set(required_gates)
        self.proof_authority = proof_authority
        (self.release_root / "staged").mkdir(parents=True, exist_ok=True)
        (self.release_root / "backups").mkdir(parents=True, exist_ok=True)

    def stage(self, files: dict[str, bytes], *, expected_current: dict[str, str | None] | None = None,
              gate_results: list[dict[str, object]] | None = None) -> StagedRelease:
        if not files:
            raise ReleaseError("cannot stage an empty release")
        if any(Path(key).is_absolute() or ".." in Path(key).parts for key in files):
            raise ReleaseError("release paths must be relative and non-traversing")
        release_id = uuid.uuid4().hex
        root = self.release_root / "staged" / release_id
        root.mkdir()
        manifest: dict[str, str] = {}
        for relative, data in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            manifest[relative] = _digest(data)
        expected_current = dict(expected_current or {})
        gate_results = list(gate_results or [])
        if gate_results:
            if self.proof_authority is None:
                raise ReleaseError("signed Gate Proof Authority is required")
            try:
                self.proof_authority.verify_gates(gate_results, subject=self.subject_for(manifest),
                                                  required=self.required_gates, input_hashes=set(manifest.values()))
            except ProofError as exc:
                raise ReleaseError(str(exc)) from exc
        unknown = set(expected_current) - set(files)
        if unknown:
            raise ReleaseError(f"target hash supplied for unstaged path: {sorted(unknown)}")
        record = {"candidate_hashes": manifest, "expected_current": expected_current,
                  "gate_results": gate_results, "status": "staged"}
        (root / "manifest.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        return StagedRelease(release_id, root, manifest, expected_current, tuple(gate_results))

    def apply(self, staged: StagedRelease, destination: str | Path) -> Path:
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if self.required_gates:
            if self.proof_authority is None:
                raise ReleaseError("signed Gate Proof Authority is required")
            try:
                self.proof_authority.verify_gates(list(staged.gate_results), subject=self.subject_for(staged.manifest),
                                                  required=self.required_gates, input_hashes=set(staged.manifest.values()))
            except ProofError as exc:
                raise ReleaseError(str(exc)) from exc
        if set(staged.expected_current) != set(staged.manifest):
            raise ReleaseError("production apply requires expected_current hash for every target")
        manifest_path = staged.root / "manifest.json"
        expected_record = {"candidate_hashes": staged.manifest, "expected_current": staged.expected_current,
                           "gate_results": list(staged.gate_results), "status": "staged"}
        if not manifest_path.is_file() or json.loads(manifest_path.read_text(encoding="utf-8")) != expected_record:
            raise ReleaseError("staged manifest changed")
        for relative, expected in staged.manifest.items():
            source = staged.root / relative
            if not source.is_file() or _digest(source.read_bytes()) != expected:
                raise ReleaseError(f"hash lock failed: {relative}")
            target = destination / relative
            # Existing symlinks in any parent component must not redirect an
            # otherwise relative release path outside the authority root.
            cursor = destination
            for part in Path(relative).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ReleaseError(f"symlink target forbidden: {relative}")
            try:
                target.resolve(strict=False).relative_to(destination)
            except ValueError as exc:
                raise ReleaseError(f"release target escapes destination: {relative}") from exc
        # Compare-and-swap protects the interval between plan and apply.  None
        # means the plan requires a previously absent target.
        for relative, expected in staged.expected_current.items():
            target = destination / relative
            actual = _digest(target.read_bytes()) if target.is_file() else None
            if actual != expected:
                raise ReleaseError(f"stale release plan: {relative}")
        backup = self.release_root / "backups" / staged.id
        journal = self.release_root / "staged" / staged.id / "transaction.json"
        journal.write_text(json.dumps({"status": "applying", "destination": str(destination)}, sort_keys=True), encoding="utf-8")
        try:
            for relative in staged.manifest:
                target = destination / relative
                if target.exists():
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, saved)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged.root / relative, target)
        except Exception as exc:
            self.rollback(staged, destination)
            raise ReleaseError(f"apply rolled back: {exc}") from exc
        journal.write_text(json.dumps({"status": "applied", "destination": str(destination)}, sort_keys=True), encoding="utf-8")
        return backup

    @staticmethod
    def subject_for(manifest: dict[str, str]) -> str:
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        return f"release:{hashlib.sha256(payload).hexdigest()}"

    def rollback(self, staged: StagedRelease, destination: str | Path) -> None:
        destination, backup = Path(destination), self.release_root / "backups" / staged.id
        for relative in staged.manifest:
            target, saved = destination / relative, backup / relative
            if saved.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, target)
            elif target.exists():
                target.unlink()
        journal = staged.root / "transaction.json"
        journal.write_text(json.dumps({"status": "rolled_back", "destination": str(destination)}, sort_keys=True), encoding="utf-8")
