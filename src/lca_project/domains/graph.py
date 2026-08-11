"""Name-graph deterministic gates."""
from __future__ import annotations

from pathlib import Path
from .base import AdapterResult, VendorAdapter


class GraphAdapter(VendorAdapter):
    domain = "graph"

    def validate(self, graph: str | Path, *, workspace: str | Path) -> AdapterResult:
        graph = self.required_file(graph, "graph")
        return self.run("validate_graph.py", [graph], workspace=workspace)

    def index(self, graph: str | Path, *, workspace: str | Path, with_io: bool = False) -> AdapterResult:
        graph = self.required_file(graph, "graph")
        args: list[str | Path] = [graph]
        if with_io:
            args.append("--with-io")
        return self.run("graph_index.py", args, workspace=workspace)

    def gate(self, journal: str | Path, slug: str, industry: str, output: str | Path, *, workspace: str | Path) -> AdapterResult:
        journal = self.required_file(journal, "journal")
        work = Path(workspace).resolve()
        out = Path(output)
        if not out.is_absolute():
            out = work / out
        return self.run("gate.py", [journal, slug, industry, "--out", out.resolve(), "--no-html"], workspace=work)
