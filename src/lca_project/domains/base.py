"""Controlled boundary for deterministic legacy-domain tools.

Adapters never edit the source project.  A caller supplies a disposable
workspace containing its inputs; every process is run there and its result is
returned as structured data for the kernel to persist as an artifact.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Sequence


class AdapterError(ValueError):
    """Raised before a domain command is allowed to start."""


@dataclass(frozen=True)
class AdapterResult:
    domain: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    workspace: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


class VendorAdapter:
    """Base class for read-only vendored scripts.

    ``workspace`` is intentionally separate from the package/vendor tree:
    scripts may create reports only beneath that workspace.
    """

    domain = "base"

    def __init__(self, vendor_root: Path | None = None) -> None:
        self.vendor_root = vendor_root or Path(__file__).resolve().parents[3] / "vendor" / "lca_cornerstone"
        self.scripts = self.vendor_root / "scripts"

    def run(
        self,
        script: str,
        args: Sequence[str | Path] = (),
        *,
        workspace: str | Path,
        timeout_seconds: int = 120,
        extra_env: Mapping[str, str] | None = None,
    ) -> AdapterResult:
        work = Path(workspace).resolve()
        if not work.is_dir():
            raise AdapterError(f"workspace does not exist: {work}")
        tool = (self.scripts / script).resolve()
        if tool.parent != self.scripts.resolve() or not tool.is_file():
            raise AdapterError(f"unregistered vendor script: {script}")
        cmd = (sys.executable, str(tool), *(str(item) for item in args))
        try:
            completed = subprocess.run(
                cmd, cwd=work, text=True, capture_output=True,
                timeout=timeout_seconds, env=dict(extra_env) if extra_env else None,
            )
            return AdapterResult(self.domain, cmd, completed.returncode, completed.stdout,
                                 completed.stderr, str(work))
        except subprocess.TimeoutExpired as exc:
            return AdapterResult(self.domain, cmd, 124, exc.stdout or "", exc.stderr or "timeout",
                                 str(work))

    @staticmethod
    def required_file(value: str | Path, label: str) -> Path:
        path = Path(value).resolve()
        if not path.is_file():
            raise AdapterError(f"{label} does not exist: {path}")
        return path
