"""Build a disposable, legacy-shaped Wiki production workspace.

The historical Wiki controller derives every input from its repository root.
This module gives it that shape using only frozen assets vendored into this
project.  It deliberately never reads, imports, or symlinks the source
``lca-cornerstone`` checkout.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Iterable


class WikiWorkspaceError(ValueError):
    """Raised when an isolated compatibility workspace cannot be proven safe."""


@dataclass(frozen=True)
class WikiWorkspace:
    root: Path
    manifest: Path
    files: int


class WikiWorkspaceBuilder:
    """Materialize the Phase 2 fixture under a caller-owned empty directory."""

    REQUIRED_TREES = (
        "scripts",
        ".claude/workflows",
        "schemas",
        "fixtures/wiki-phase2",
    )

    def __init__(self, vendor_root: Path | None = None) -> None:
        self.vendor_root = (vendor_root or Path(__file__).resolve().parents[3]
                            / "vendor" / "lca_cornerstone").resolve()

    @staticmethod
    def _digest(path: Path) -> str:
        return sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _ensure_real_tree(path: Path) -> None:
        if path.is_symlink():
            raise WikiWorkspaceError(f"symlink is forbidden: {path}")
        if not path.exists() or not path.is_dir():
            raise WikiWorkspaceError(f"missing fixture tree: {path}")
        for item in path.rglob("*"):
            if item.is_symlink():
                raise WikiWorkspaceError(f"symlink is forbidden: {item}")

    def _source_files(self) -> Iterable[Path]:
        for relative in self.REQUIRED_TREES:
            root = self.vendor_root / relative
            self._ensure_real_tree(root)
            yield from (
                path for path in sorted(root.rglob("*"))
                if path.is_file() and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            )

    def _validate_destination(self, destination: Path) -> Path:
        raw_root = destination.absolute()
        if raw_root.is_symlink():
            raise WikiWorkspaceError(f"workspace cannot be a symlink: {raw_root}")
        # /tmp is a system symlink on macOS.  Resolve it before checking the
        # containment boundary; the workspace itself remains a real directory.
        root = raw_root.resolve()
        if root == self.vendor_root or self.vendor_root in root.parents:
            raise WikiWorkspaceError("workspace must not be created inside vendor assets")
        if root.exists() and any(root.iterdir()):
            raise WikiWorkspaceError(f"workspace must be empty: {root}")
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def build(self, destination: str | Path) -> WikiWorkspace:
        """Copy frozen scripts and fixture inputs into ``destination``.

        A new ``workspace-manifest.json`` binds every installed file to both
        its vendored origin and SHA-256.  The result contains no symlink and
        no path pointing at the source checkout.
        """
        root = self._validate_destination(Path(destination))
        records: list[dict[str, str]] = []
        fixture = self.vendor_root / "fixtures/wiki-phase2"
        for source in self._source_files():
            relative = source.relative_to(self.vendor_root)
            if relative.is_relative_to(Path("fixtures/wiki-phase2")):
                target_relative = relative.relative_to("fixtures/wiki-phase2")
            else:
                target_relative = relative
            target = root / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            shutil.copystat(source, target)
            records.append({
                "path": target_relative.as_posix(),
                "vendor_path": relative.as_posix(),
                "sha256": self._digest(target),
            })
        # This is intentionally a fresh workspace-local document, not a copy
        # of an upstream journal; it establishes the immutable input boundary.
        manifest = root / "workspace-manifest.json"
        manifest.write_text(json.dumps({
            "protocol": {"version": "wiki-workspace-v1", "kind": "input-manifest"},
            "origin": "lca-project/vendor/lca_cornerstone (frozen copy)",
            "source_checkout_access": False,
            "fixture": "wiki-phase2",
            "files": records,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return WikiWorkspace(root=root, manifest=manifest, files=len(records))

    def verify(self, workspace: str | Path) -> WikiWorkspace:
        """Check copied assets against the vendored snapshot, not the source repo."""
        root = Path(workspace).resolve()
        manifest = root / "workspace-manifest.json"
        if not manifest.is_file():
            raise WikiWorkspaceError("workspace-manifest.json is missing")
        document = json.loads(manifest.read_text(encoding="utf-8"))
        if document.get("protocol", {}).get("version") != "wiki-workspace-v1":
            raise WikiWorkspaceError("unsupported workspace manifest")
        records = document.get("files")
        if not isinstance(records, list) or not records:
            raise WikiWorkspaceError("workspace manifest has no files")
        for record in records:
            target = root / str(record.get("path", ""))
            vendor = self.vendor_root / str(record.get("vendor_path", ""))
            if (not target.is_file() or target.is_symlink() or not vendor.is_file()
                    or target.resolve() == vendor.resolve()):
                raise WikiWorkspaceError(f"invalid isolated asset: {target}")
            digest = str(record.get("sha256", ""))
            if self._digest(target) != digest or self._digest(vendor) != digest:
                raise WikiWorkspaceError(f"asset hash drift: {target}")
        return WikiWorkspace(root=root, manifest=manifest, files=len(records))
