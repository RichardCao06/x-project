"""Hash-bound recovery for task-owned Wiki materializations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor/lca_cornerstone/scripts"))

from merge_wiki_ku import (  # noqa: E402
    BODY_RE,
    MergeError,
    apply_plan,
    rehydrate_committed_plan,
)
from wiki_batch import (  # noqa: E402
    committed_transaction,
    file_record,
    init_journal,
    transition_journal,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    page = tmp_path / "wiki/ict/activities/A039--test.md"
    registry = tmp_path / "sources/ict/registry.json"
    ku = tmp_path / "runs/kus.json"
    plan = tmp_path / "runs/batch-merge-plan.json"
    page.parent.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    ku.parent.mkdir(parents=True)
    before = """---
id: A039
body_status: empty
---
<!-- BODY:START -->
old body
<!-- BODY:END -->
"""
    page.write_text(before, encoding="utf-8")
    registry.write_text(json.dumps({"sources": {}}, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    ku.write_text(json.dumps({"kus": [{
        "ku_id": "ku-test", "claim_id": "A039-0", "authority": "draft",
    }]}), encoding="utf-8")
    body = "\nold body\n"
    plan.write_text(json.dumps({
        "protocol": {"version": "wiki-ku-v1", "kind": "wiki-ku-merge-plan"},
        "plan_mode": "batch",
        "ku_path": str(ku), "ku_sha256": _sha(ku.read_text(encoding="utf-8")),
        "wiki_root": str(page.parents[2]),
        "registry_path": str(registry),
        "registry_sha256": _sha(registry.read_text(encoding="utf-8")),
        "registry_entries": {},
        "files": [{
            "node_id": "A039", "write_mode": "rebuild", "path": str(page),
            "file_sha256": _sha(before), "body_sha256": _sha(body),
            "operations": [{"claim_id": "A039-0", "ku_id": "ku-test",
                            "action": "insert_body_claim", "authority": "draft"}],
            "manual_review": [], "replacement_body": "\nrestored body\n",
            "replacement_evidence": None,
            "changelog": "- restored from frozen plan",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    return plan, page, before


def test_rehydrate_replays_only_the_exact_original_seed_state(tmp_path: Path) -> None:
    plan, page, before = _fixture(tmp_path)
    apply_plan(plan)
    materialized = page.read_text(encoding="utf-8")
    assert materialized != before

    page.write_text(before, encoding="utf-8")
    result = rehydrate_committed_plan(plan)

    assert result["rehydrated"] is True
    assert page.read_text(encoding="utf-8") == materialized
    assert (plan.parent / "apply-transaction-pre-rehydrate.json").is_file()


def test_rehydrate_rejects_unrecognized_physical_state(tmp_path: Path) -> None:
    plan, page, _ = _fixture(tmp_path)
    apply_plan(plan)
    page.write_text("third-party state\n", encoding="utf-8")

    with pytest.raises(MergeError, match="unrecognized hash"):
        rehydrate_committed_plan(plan)


def test_replaced_plan_path_invalidates_and_namespaces_prior_transaction(
    tmp_path: Path,
) -> None:
    plan, page, _ = _fixture(tmp_path)
    first_plan_sha256 = _sha(plan.read_text(encoding="utf-8"))
    apply_plan(plan)
    first_materialized = page.read_text(encoding="utf-8")
    transaction_path = plan.parent / "apply-transaction.json"
    first_transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert first_transaction["plan_sha256"] == first_plan_sha256

    replacement = json.loads(plan.read_text(encoding="utf-8"))
    body = BODY_RE.search(first_materialized)
    assert body is not None
    replacement["files"][0].update({
        "file_sha256": _sha(first_materialized),
        "body_sha256": _sha(body.group(1)),
        "replacement_body": "\ncurrent generation body\n",
        "changelog": "- current generation",
    })
    plan.write_text(json.dumps(replacement, ensure_ascii=False), encoding="utf-8")
    current_plan_sha256 = _sha(plan.read_text(encoding="utf-8"))

    with pytest.raises(MergeError, match="not committed for the frozen plan"):
        rehydrate_committed_plan(plan)
    with pytest.raises(ValueError, match="不属于当前 merge plan"):
        committed_transaction(transaction_path, expected_plan=plan)
    assert page.read_text(encoding="utf-8") == first_materialized

    result = apply_plan(plan)
    current_transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert result["transaction"] == "committed"
    assert current_transaction["plan_sha256"] == current_plan_sha256
    assert _sha(page.read_text(encoding="utf-8")) == next(
        item["new_sha256"] for item in current_transaction["targets"]
        if Path(item["target"]) == page
    )
    archived = plan.parent / f"apply-transaction-{first_plan_sha256[:12]}.json"
    assert json.loads(archived.read_text(encoding="utf-8")) == first_transaction


def test_cross_generation_unfinished_transaction_fails_closed(tmp_path: Path) -> None:
    plan, page, before = _fixture(tmp_path)
    transaction_path = plan.parent / "apply-transaction.json"
    transaction_path.write_text(json.dumps({
        "protocol": {"version": "wiki-ku-transaction-v1"},
        "state": "committing",
        "plan": str(plan),
        "plan_sha256": "0" * 64,
        "targets": [],
    }), encoding="utf-8")

    with pytest.raises(MergeError, match="different plan generation"):
        apply_plan(plan)
    assert page.read_text(encoding="utf-8") == before


def test_legacy_path_only_commit_cannot_authorize_current_plan(tmp_path: Path) -> None:
    plan, page, before = _fixture(tmp_path)
    transaction_path = plan.parent / "apply-transaction.json"
    transaction_path.write_text(json.dumps({
        "protocol": {"version": "wiki-ku-transaction-v1"},
        "state": "committed",
        "plan": str(plan),
        "targets": [{
            "target": str(page), "old_sha256": "1" * 64,
            "new_sha256": "2" * 64,
        }],
    }), encoding="utf-8")

    result = apply_plan(plan)

    current = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert result["transaction"] == "committed"
    assert current["plan_sha256"] == _sha(plan.read_text(encoding="utf-8"))
    assert current["targets"][0]["old_sha256"] == _sha(before)
    assert list(plan.parent.glob("apply-transaction-legacy-*.json"))


def test_research_rewind_invalidates_plan_descendant_receipt_and_transaction(
    tmp_path: Path,
) -> None:
    batch = tmp_path / "runs/batch"
    batch.mkdir(parents=True)
    manifest = batch / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    init_journal(batch, manifest)
    research = batch / "research-ready.json"
    research.write_text("{}\n", encoding="utf-8")
    content = batch / "content-apply-report.json"
    plan_sha256 = "a" * 64
    content.write_text(json.dumps({
        "plan": {"path": str(batch / "batch-merge-plan.json"),
                 "sha256": plan_sha256},
    }), encoding="utf-8")
    transaction = batch / "apply-transaction.json"
    transaction.write_text(json.dumps({
        "state": "committed", "plan_sha256": plan_sha256,
    }), encoding="utf-8")
    journal_path = batch / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["state"] = "frozen"
    journal["artifacts"].update({
        "verified": {"path": "obsolete"},
        "frozen": {"path": "obsolete"},
        "content_apply": file_record(content),
    })
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    rewound = transition_journal(
        batch, "research_ready", "research-ready", resume=True,
        repair_rewind=True, artifacts={"research_ready": file_record(research)},
    )

    assert rewound["state"] == "research_ready"
    assert set(rewound["artifacts"]) == {"research_ready"}
    assert not content.exists() and not transaction.exists()
    assert (batch / f"content-apply-report-{plan_sha256[:12]}.json").is_file()
    assert (batch / f"apply-transaction-{plan_sha256[:12]}.json").is_file()
