"""Machine-readable acceptance coverage.

This is deliberately a traceability check, not a claim that evaluation or
human/canary work has been executed.  Those modes keep explicit runners and
required evidence in ``traceability.json``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACE = json.loads((ROOT / "tests" / "traceability.json").read_text(encoding="utf-8"))


def test_all_96_design_cases_are_traceable_once() -> None:
    html = (ROOT / "docs" / "测试设计-骨架数据库自治生产平台.html").read_text(encoding="utf-8")
    documented = set(re.findall(r">((?:CTL|ART|EVT|CAP|WF|AGT|GRF|WIKI|XLC|BOM|REL|E2E)-\d{3})<", html))
    tracked = {f"{suite}-{number:03d}" for suite, modes in TRACE["suites"].items() for number in range(1, len(modes) + 1)}
    assert len(documented) == len(tracked) == 96
    assert documented == tracked


def test_all_case_execution_modes_have_a_declared_route() -> None:
    """One metadata test; never inflate the behavioral pass count by 96."""
    errors = []
    for suite, modes in TRACE["suites"].items():
        for number, mode in enumerate(modes, 1):
            case_id = f"{suite}-{number:03d}"
            if mode in {"EVAL", "SHADOW", "CANARY", "MANUAL"}:
                if case_id not in TRACE["non_automated_contracts"]:
                    errors.append(f"{case_id}: non-automated contract missing")
            elif not TRACE["implemented_by"][suite].startswith("tests/"):
                errors.append(f"{case_id}: test route missing")
    assert not errors, errors
