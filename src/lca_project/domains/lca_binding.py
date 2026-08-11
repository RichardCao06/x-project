"""Dataset-association policy gate; it never performs calculations itself."""
from __future__ import annotations

from .base import AdapterResult, VendorAdapter


class LcaBindingAdapter(VendorAdapter):
    domain = "lca_binding"

    def validate(self, *, workspace: str, scope: str | None = None, node: str | None = None) -> AdapterResult:
        args: list[str] = []
        if scope:
            args += ["--scope", scope]
        if node:
            args += ["--node", node]
        return self.run("validate_lca_dataset_binding.py", args, workspace=workspace)
