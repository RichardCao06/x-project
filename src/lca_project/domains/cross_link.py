"""Cross-industry binding preparation, deterministic application and lint."""
from __future__ import annotations

from pathlib import Path
from .base import AdapterResult, VendorAdapter


class CrossLinkAdapter(VendorAdapter):
    domain = "cross_link"

    def prepare(self, slug: str, *, workspace: str | Path) -> AdapterResult:
        return self.run("prep_cross_link.py", [slug], workspace=workspace)

    def apply(self, nominations: str | Path, *, workspace: str | Path, dry_run: bool = True) -> AdapterResult:
        nominations = self.required_file(nominations, "nominations")
        args: list[str | Path] = [nominations]
        if dry_run:
            args.append("--dry-run")
        return self.run("apply_cross_link.py", args, workspace=workspace)

    def lint(self, *, workspace: str | Path) -> AdapterResult:
        return self.run("cross_link_lint.py", workspace=workspace)
