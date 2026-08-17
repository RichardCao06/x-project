from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_wiki_draft_content_gate", ROOT / "scripts/run_wiki_draft_content_gate.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def report() -> dict:
    return {"go": False, "disposition": "blocked_before_content_apply", "pages": [{
        "go": False, "checks": {
            "assertions_rich_enough": True,
            "cited_content_rich_enough": False,
            "source_diversity": False,
            "core_sections_source_grounded": False,
            "citations_resolve": True,
        },
    }]}


def test_preview_preserves_failed_checks_and_requests_repair() -> None:
    result = MODULE.apply_publication_policy(report(), "preview")
    assert result["go"] is False
    assert result["pipeline_continue"] is False
    assert result["decision"] == "REPAIR"
    assert result["pages"][0]["checks"]["source_diversity"] is False
    assert result["pages"][0]["quality_warnings"] == [
        "cited_content_rich_enough", "core_sections_source_grounded", "source_diversity",
    ]


def test_preview_can_continue_as_limited_without_becoming_candidate() -> None:
    result = MODULE.apply_publication_policy(
        report(), "preview",
        source_gate={"decision": "LIMITED"},
        closure_gate={"decision": "LIMITED"},
    )
    assert result["go"] is False
    assert result["pipeline_continue"] is True
    assert result["decision"] == "LIMITED"
    assert result["candidate_eligible"] is False


def test_reviewed_retains_strict_coverage_gate() -> None:
    result = MODULE.apply_publication_policy(report(), "reviewed")
    assert result["go"] is False


def test_preview_does_not_downgrade_hard_content_failure() -> None:
    value = report()
    value["pages"][0]["checks"]["citations_resolve"] = False
    result = MODULE.apply_publication_policy(value, "preview")
    assert result["go"] is False
