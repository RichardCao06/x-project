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
        "profiles",
        ".claude/workflows",
        "schemas",
        "fixtures/wiki-phase2",
    )
    # Fixture pages and source registries are immutable only until a Job is
    # materialized.  Apply tasks own these paths afterwards, so a code repair
    # must never project the vendor seed over their live state.
    BOOTSTRAP_ONLY_PREFIXES = ("wiki/", "sources/")

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

    def _source_records(self) -> list[tuple[Path, Path]]:
        records: list[tuple[Path, Path]] = []
        for source in self._source_files():
            relative = source.relative_to(self.vendor_root)
            target_relative = (
                relative.relative_to("fixtures/wiki-phase2")
                if relative.is_relative_to(Path("fixtures/wiki-phase2"))
                else relative
            )
            records.append((source, target_relative))
        return records

    @classmethod
    def _refresh_policy(cls, target_relative: Path | str) -> str:
        logical = Path(target_relative).as_posix()
        return ("bootstrap_only" if logical.startswith(cls.BOOTSTRAP_ONLY_PREFIXES)
                else "immutable_refreshable")

    def _manifest_record(self, source: Path, target_relative: Path) -> dict[str, str]:
        return {
            "path": target_relative.as_posix(),
            "vendor_path": source.relative_to(self.vendor_root).as_posix(),
            "sha256": self._digest(source),
            "refresh_policy": self._refresh_policy(target_relative),
        }

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
        for source, target_relative in self._source_records():
            relative = source.relative_to(self.vendor_root)
            target = root / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            shutil.copystat(source, target)
            records.append(self._manifest_record(source, target_relative))
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

    def refresh(
        self, workspace: str | Path, *, vendor_paths: Iterable[str] | None = None
    ) -> WikiWorkspace:
        """Refresh managed frozen inputs while preserving run artifacts.

        Repairing a long-lived Job must not keep executing scripts copied by an
        older Dashboard process.  Only files owned by the workspace manifest
        are replaced or removed; generated ``runs`` and other outputs remain
        untouched.
        """
        root = Path(workspace).resolve()
        manifest = root / "workspace-manifest.json"
        if not manifest.is_file():
            raise WikiWorkspaceError("workspace-manifest.json is missing")
        document = json.loads(manifest.read_text(encoding="utf-8"))
        if document.get("protocol", {}).get("version") != "wiki-workspace-v1":
            raise WikiWorkspaceError("unsupported workspace manifest")
        selected = None if vendor_paths is None else {
            Path(value).as_posix().removeprefix("vendor/lca_cornerstone/")
            for value in vendor_paths
        }
        prior = {
            str(record.get("path", "")): dict(record)
            for record in document.get("files", [])
            if isinstance(record, dict) and str(record.get("path", ""))
        }
        records: list[dict[str, str]] = []
        for source, target_relative in self._source_records():
            record = self._manifest_record(source, target_relative)
            target = root / target_relative
            should_refresh = record["refresh_policy"] == "immutable_refreshable"
            if selected is not None:
                should_refresh = should_refresh and record["vendor_path"] in selected
            if should_refresh:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                shutil.copystat(source, target)
            elif target_relative.as_posix() in prior:
                record = prior[target_relative.as_posix()]
                record["refresh_policy"] = self._refresh_policy(target_relative)
            elif not target.exists():
                # A newly vendored path has no live owner yet.  Seed it once;
                # later refreshes will respect its ownership class.
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                shutil.copystat(source, target)
            records.append(record)
        manifest.write_text(json.dumps({
            "protocol": {"version": "wiki-workspace-v1", "kind": "input-manifest"},
            "origin": "lca-project/vendor/lca_cornerstone (frozen copy)",
            "source_checkout_access": False,
            "fixture": "wiki-phase2",
            "files": records,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.verify(root)

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
        expected = {
            target.as_posix(): source.relative_to(self.vendor_root).as_posix()
            for source, target in self._source_records()
        }
        declared = {
            str(record.get("path", "")): str(record.get("vendor_path", ""))
            for record in records if isinstance(record, dict)
        }
        if declared != expected:
            missing = sorted(set(expected) - set(declared))
            extra = sorted(set(declared) - set(expected))
            raise WikiWorkspaceError(
                f"workspace manifest tree drift: missing={missing} extra={extra}"
            )
        for record in records:
            target = root / str(record.get("path", ""))
            vendor = self.vendor_root / str(record.get("vendor_path", ""))
            if (not target.is_file() or target.is_symlink() or not vendor.is_file()
                    or target.resolve() == vendor.resolve()):
                raise WikiWorkspaceError(f"invalid isolated asset: {target}")
            digest = str(record.get("sha256", ""))
            policy = str(record.get("refresh_policy") or
                         self._refresh_policy(str(record.get("path", ""))))
            if policy == "immutable_refreshable" and self._digest(target) != digest:
                raise WikiWorkspaceError(f"asset hash drift: {target}")
            if policy not in {"immutable_refreshable", "bootstrap_only"}:
                raise WikiWorkspaceError(f"unsupported refresh policy: {policy}")
        return WikiWorkspace(root=root, manifest=manifest, files=len(records))
