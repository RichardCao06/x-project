"""Wiki-v2 release transaction adapter.

Only the deterministic batch controller may mutate a supplied compatibility
workspace.  Proposal and Verify agent outputs are inputs, never executable
instructions.
"""
from __future__ import annotations

from pathlib import Path
import ipaddress
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlsplit
from .base import AdapterError, AdapterResult, VendorAdapter


class WikiAdapter(VendorAdapter):
    domain = "wiki"

    @staticmethod
    def validate_external_url(url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("external evidence URL must be credential-free http(s)")
        if parsed.port not in {None, 80, 443}:
            raise ValueError("non-standard evidence URL port")
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("local evidence host forbidden")
        try:
            addresses = [ipaddress.ip_address(hostname)]
        except ValueError:
            try:
                addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(hostname, parsed.port or 443)]
            except socket.gaierror:
                addresses = []  # locator validation may precede deterministic fetch
        if any(not address.is_global for address in addresses):
            raise ValueError("private, loopback or reserved evidence address")
        return url

    def _run_compat(self, script: str, args: list[str | Path], *, workspace: str | Path,
                    timeout_seconds: int = 120) -> AdapterResult:
        """Run the frozen controller from a disposable legacy-shaped root.

        The old controller resolves `docs/`, `wiki/` and workflow templates
        relative to its own script.  Copying only frozen executables/templates
        into the caller-owned workspace preserves that contract without giving
        it a path back to the source or vendor trees.
        """
        work = Path(workspace).resolve()
        if not work.is_dir():
            raise AdapterError(f"workspace does not exist: {work}")
        scripts = work / "scripts"
        workflows = work / ".claude" / "workflows"
        shutil.copytree(self.scripts, scripts, dirs_exist_ok=True)
        shutil.copytree(self.vendor_root / ".claude" / "workflows", workflows, dirs_exist_ok=True)
        tool = scripts / script
        cmd = (sys.executable, str(tool), *(str(item) for item in args))
        try:
            completed = subprocess.run(cmd, cwd=work, text=True, capture_output=True, timeout=timeout_seconds)
            return AdapterResult(self.domain, cmd, completed.returncode, completed.stdout, completed.stderr, str(work))
        except subprocess.TimeoutExpired as exc:
            return AdapterResult(self.domain, cmd, 124, exc.stdout or "", exc.stderr or "timeout", str(work))

    def plan(self, industry: str, nodes: list[str], *, workspace: str | Path, output: str | Path,
             dry_run: bool = True, batch_id: str | None = None) -> AdapterResult:
        if not nodes:
            raise AdapterError("at least one node is required")
        args: list[str | Path] = ["plan", industry]
        if batch_id:
            args += ["--batch-id", batch_id]
        for node in nodes:
            args += ["--nodes", node]
        out = Path(output)
        if not out.is_absolute():
            out = Path(workspace).resolve() / out
        args += ["--output", out.resolve()]
        if dry_run:
            args.append("--dry-run")
        return self._run_compat("wiki_batch.py", args, workspace=workspace)

    def command(self, stage: str, manifest: str | Path, *, workspace: str | Path, dry_run: bool = True) -> AdapterResult:
        allowed = {"prepare", "validate", "finalize", "go-no-go", "gate", "publish"}
        if stage not in allowed:
            raise AdapterError(f"wiki stage is not allowed through generic adapter: {stage}")
        args: list[str | Path] = [stage, self.required_file(manifest, "wiki manifest")]
        if dry_run:
            args.append("--dry-run")
        return self._run_compat("wiki_batch.py", args, workspace=workspace)
