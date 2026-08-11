"""Publication contract shared by kernel release manager and domain adapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib


@dataclass(frozen=True)
class PublicationCandidate:
    path: Path
    sha256: str
    kind: str
    gate_artifact: str

    @classmethod
    def from_file(cls, path: str | Path, *, kind: str, gate_artifact: str) -> "PublicationCandidate":
        target = Path(path).resolve()
        if not target.is_file():
            raise FileNotFoundError(target)
        return cls(target, hashlib.sha256(target.read_bytes()).hexdigest(), kind, gate_artifact)

    def verify_unchanged(self) -> bool:
        return hashlib.sha256(self.path.read_bytes()).hexdigest() == self.sha256
