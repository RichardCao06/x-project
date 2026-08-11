"""Phase-2 historical Wiki defect replays.

These are behavioural tests: each mutation is passed to an executable gate or
transaction boundary.  The xfails identify intentionally unimplemented
production boundaries; they are strict so implementing a boundary turns an
unexpected pass into a prompt to remove the marker and strengthen its oracle.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lca_project.domains.wiki import WikiAdapter
from lca_project.kernel.assurance import AssuranceError, gate_claim_evidence, gate_identity
from lca_project.kernel.release import ReleaseError, ReleaseManager


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "defects" / "wiki"


def defect(name: str) -> dict[str, object]:
    return json.loads((CORPUS / name).read_text(encoding="utf-8"))


def test_wiki_001_verified_source_does_not_verify_an_unchecked_claim() -> None:
    """WIKI-001: a trusted source cannot be used as a page-level shortcut."""
    item = defect("source-not-claim.json")
    source = item["source"]
    assert source["verified"] is True
    with pytest.raises(AssuranceError, match="literal"):
        gate_claim_evidence(item["claim"], source["payload"])


def test_wiki_adjacent_evidence_cannot_be_promoted_to_confirmed() -> None:
    """AGT-005 / E2E-002: the G4 production boundary rejects ADJACENT."""
    item = defect("adjacent-evidence.json")
    with pytest.raises(AssuranceError, match="EXACT"):
        gate_claim_evidence(item["claim"], item["source_payload"])


def test_wiki_identity_swap_is_rejected_before_content_is_consumed() -> None:
    """G1 is a hard join, never a best-effort normalization."""
    item = defect("identity-swap.json")
    with pytest.raises(AssuranceError, match="node_ref"):
        gate_identity(item["frozen"], item["agent_output"])


def test_wiki_old_gate_result_cannot_authorize_new_candidate(tmp_path: Path) -> None:
    """REL-003 / WIKI mutation M-GATE-REUSE is blocked at staging."""
    item = defect("old-gate.json")
    manager = ReleaseManager(tmp_path / "releases", required_gates={"G6", "G7"})
    with pytest.raises(ReleaseError, match="signed Gate Proof Authority"):
        manager.stage({"wiki/P042.md": item["candidate"].encode()}, gate_results=item["gates"])


def test_wiki_apply_crash_rolls_back_already_written_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REL transaction has no partial publish when the second write crashes."""
    destination = tmp_path / "production"
    destination.mkdir()
    first, second = destination / "wiki/P042.md", destination / "wiki/P043.md"
    first.parent.mkdir()
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    manager = ReleaseManager(tmp_path / "releases", required_gates=set())
    staged = manager.stage(
        {"wiki/P042.md": b"new first", "wiki/P043.md": b"new second"},
        expected_current={
            "wiki/P042.md": hashlib.sha256(b"old first").hexdigest(),
            "wiki/P043.md": hashlib.sha256(b"old second").hexdigest(),
        },
    )
    from lca_project.kernel import release as release_module

    real_copy = release_module.shutil.copy2

    def crash_on_second(source: str | Path, target: str | Path, *args: object, **kwargs: object) -> object:
        if Path(source) == staged.root / "wiki/P043.md":
            raise OSError("injected apply crash")
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(release_module.shutil, "copy2", crash_on_second)
    with pytest.raises(ReleaseError, match="rolled back"):
        manager.apply(staged, destination)
    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"old second"
    assert json.loads((staged.root / "transaction.json").read_text()) ["status"] == "rolled_back"


def test_wiki_006_coverage_denominator_is_conserved() -> None:
    """WIKI-006: NOT_FOUND, BUDGET_SKIPPED and UNMAPPED remain in coverage."""
    item = defect("coverage-drop.json")
    from lca_project.domains.wiki_coverage import validate_coverage  # type: ignore[import-not-found]

    report = validate_coverage(item)
    assert report["coverage_set"] == set(item["required_claim_ids"])
    assert report["reviewed_upgrade_allowed"] is False


@pytest.mark.parametrize("url", json.loads((CORPUS / "unsafe-urls.json").read_text()))
def test_wiki_003_external_evidence_protocol_rejects_local_and_private_urls(url: str) -> None:
    """WIKI-003 requires both fetch and result validation to fail closed."""
    adapter = WikiAdapter()
    with pytest.raises(ValueError):
        adapter.validate_external_url(url)  # type: ignore[attr-defined]


def test_wiki_002_shared_footnote_keeps_each_factual_sentence_in_denominator() -> None:
    text = (CORPUS / "shared-footnote.md").read_text(encoding="utf-8")
    from lca_project.domains.wiki_coverage import factual_claims  # type: ignore[import-not-found]

    claims = factual_claims(text)
    assert len(claims) == 3
    assert all(claim["citations"] == ["source-1"] for claim in claims)


def test_wiki_004_generic_gap_shell_is_blocked_before_any_page_write(tmp_path: Path) -> None:
    """Draft Gate rejects the shell before any transaction is opened."""
    page = tmp_path / "wiki" / "ict_equipment" / "P042.md"
    page.parent.mkdir(parents=True)
    original = b"trusted production body"
    page.write_bytes(original)
    from lca_project.domains.wiki_content import validate_draft_content

    candidate = (CORPUS / "generic-gap-shell.md").read_text(encoding="utf-8")
    result = validate_draft_content(candidate)
    assert result["go"] is False
    assert result["blocked_before_content_apply"] is True
    assert "repeated_evidence_gap_shell" in result["violations"]
    assert page.read_bytes() == original


def test_wiki_005_product_page_rejects_activity_only_flows_emissions_and_allocation() -> None:
    from lca_project.domains.wiki_content import validate_page_contract  # type: ignore[import-not-found]

    report = validate_page_contract((CORPUS / "product-activity-confusion.md").read_text(encoding="utf-8"))
    assert report["go"] is False
    assert "activity_only_content" in report["violations"]


def test_wiki_007_golden_regression_is_rejected_even_when_hashes_are_recomputed() -> None:
    from lca_project.domains.wiki_content import compare_to_golden  # type: ignore[import-not-found]

    candidate = (CORPUS / "golden-regression.md").read_text(encoding="utf-8")
    report = compare_to_golden(golden="rich baseline with tables and judgments", candidate=candidate)
    assert report["go"] is False


def test_wiki_008_preview_renderer_cannot_reorder_unique_sections_or_claim_production() -> None:
    from lca_project.domains.wiki_content import validate_preview  # type: ignore[import-not-found]

    report = validate_preview((CORPUS / "reordered-preview.html").read_text(encoding="utf-8"), node_type="product")
    assert report["go"] is False
    assert report["production"] is False
