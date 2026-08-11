from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest

from lca_project.control import ControlPlane


# Phase-2 regression tests intentionally import the frozen production scripts
# as modules.  Keep that compatibility surface explicit and project-local;
# never rely on a developer's source checkout or ambient PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
VENDORED_SCRIPTS = ROOT / "vendor" / "lca_cornerstone" / "scripts"
if str(VENDORED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(VENDORED_SCRIPTS))


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """An isolated project root: acceptance tests never write the source repo."""
    root = tmp_path / "platform"
    root.mkdir()
    return root


@pytest.fixture
def plane(project_root: Path) -> ControlPlane:
    return ControlPlane(project_root)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
