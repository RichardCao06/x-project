"""BOM completeness probe adapters (LLM nomination remains outside this boundary)."""
from __future__ import annotations

from pathlib import Path
from .base import AdapterResult, VendorAdapter


class BomAdapter(VendorAdapter):
    domain = "bom"

    def prepare(self, bom: str | Path, slug: str, *, workspace: str | Path, host: str | None = None) -> AdapterResult:
        bom = self.required_file(bom, "BOM")
        args: list[str | Path] = [bom, slug]
        if host:
            args += ["--host", host]
        return self.run("prep_bom_buckets.py", args, workspace=workspace)

    def grade(self, matches: str | Path, vehicle: str, buckets: str | Path, *, workspace: str | Path) -> AdapterResult:
        return self.run("grade_bom_matches.py", [self.required_file(matches, "matches"), vehicle,
                                                   self.required_file(buckets, "buckets")], workspace=workspace)

    def coverage(self, graph: str | Path, *, workspace: str | Path) -> AdapterResult:
        return self.run("check_bom_coverage.py", [self.required_file(graph, "graph")], workspace=workspace)
