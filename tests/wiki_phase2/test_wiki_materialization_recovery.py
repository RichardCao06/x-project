"""Hash-bound recovery for task-owned Wiki materializations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from lca_project.capability_runtime import _draft_apply_recovery_args


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor/lca_cornerstone/scripts"))

from merge_wiki_ku import MergeError, apply_plan, rehydrate_committed_plan  # noqa: E402


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
        "registry_entries": {"ku-test": {
            "url": "https://example.test/source", "title": "Test source",
        }},
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
    transaction = json.loads(
        (plan.parent / "apply-transaction.json").read_text(encoding="utf-8")
    )
    assert materialized != before
    assert transaction["plan_sha256"] == _sha(plan.read_text(encoding="utf-8"))

    page.write_text(before, encoding="utf-8")
    registry = Path(json.loads(plan.read_text(encoding="utf-8"))["registry_path"])
    registry.write_text(json.dumps({"sources": {}}, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    result = rehydrate_committed_plan(plan)

    assert result["rehydrated"] is True
    assert page.read_text(encoding="utf-8") == materialized
    assert (plan.parent / (
        f"apply-transaction-{transaction['plan_sha256'][:12]}-pre-rehydrate.json"
    )).is_file()


def test_rehydrate_rejects_unrecognized_physical_state(tmp_path: Path) -> None:
    plan, page, _ = _fixture(tmp_path)
    apply_plan(plan)
    page.write_text("third-party state\n", encoding="utf-8")

    with pytest.raises(MergeError, match="unrecognized hash"):
        rehydrate_committed_plan(plan)


def test_matching_plan_committed_output_is_an_idempotent_noop(tmp_path: Path) -> None:
    plan, page, _ = _fixture(tmp_path)
    apply_plan(plan)
    materialized = page.read_text(encoding="utf-8")

    result = rehydrate_committed_plan(plan)

    assert result["already_materialized"] is True
    assert result["target_classification"] == "matching_plan_output"
    assert page.read_text(encoding="utf-8") == materialized


def test_same_path_changed_plan_gets_a_fresh_digest_bound_transaction(
    tmp_path: Path,
) -> None:
    plan, page, _ = _fixture(tmp_path)
    apply_plan(plan)
    first_transaction_path = plan.parent / "apply-transaction.json"
    first_transaction = json.loads(first_transaction_path.read_text(encoding="utf-8"))
    first_plan_digest = first_transaction["plan_sha256"]

    # Model a task-owned descendant materialization and regenerate the plan in
    # place with that exact state as the new immutable seed.
    descendant = page.read_text(encoding="utf-8").replace(
        "restored body", "restored body\n\n| field | value |\n|---|---|\n| x | y |"
    )
    page.write_text(descendant, encoding="utf-8")
    registry = Path(json.loads(plan.read_text(encoding="utf-8"))["registry_path"])
    regenerated = json.loads(plan.read_text(encoding="utf-8"))
    regenerated["registry_sha256"] = _sha(registry.read_text(encoding="utf-8"))
    regenerated["registry_entries"]["ku-next"] = {
        "url": "https://example.test/next", "title": "Next source",
    }
    regenerated["files"][0]["file_sha256"] = _sha(descendant)
    body = descendant.split("<!-- BODY:START -->", 1)[1].split(
        "<!-- BODY:END -->", 1
    )[0]
    regenerated["files"][0]["body_sha256"] = _sha(body)
    regenerated["files"][0]["replacement_body"] = "\nnew generation body\n"
    plan.write_text(json.dumps(regenerated, ensure_ascii=False), encoding="utf-8")
    current_plan_digest = _sha(plan.read_text(encoding="utf-8"))

    with pytest.raises(MergeError, match="not committed for the frozen plan"):
        rehydrate_committed_plan(plan)
    result = apply_plan(plan)

    assert result["transaction"] == "committed"
    current = json.loads(first_transaction_path.read_text(encoding="utf-8"))
    assert current["plan_sha256"] == current_plan_digest
    assert current["plan_sha256"] != first_plan_digest
    assert all(item["pre_apply_state"] == "current_plan_seed"
               for item in current["targets"])
    archived = plan.parent / f"apply-transaction-{first_plan_digest[:12]}.json"
    assert json.loads(archived.read_text(encoding="utf-8")) == first_transaction
    registry_target = next(item for item in current["targets"]
                           if Path(item["target"]) == registry)
    assert _sha(registry.read_text(encoding="utf-8")) == registry_target["new_sha256"]


def test_runtime_rehydrates_only_a_transaction_for_current_plan_bytes(
    tmp_path: Path,
) -> None:
    plan, _, _ = _fixture(tmp_path)
    apply_plan(plan)
    gate = {
        "plan": str(plan.resolve()),
        "plan_sha256": _sha(plan.read_text(encoding="utf-8")),
    }
    (plan.parent / "draft-content-gate.json").write_text(
        json.dumps(gate), encoding="utf-8"
    )
    assert _draft_apply_recovery_args(plan.parent) == ["--rehydrate"]

    changed = json.loads(plan.read_text(encoding="utf-8"))
    changed["generation_marker"] = "regenerated-in-place"
    plan.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    gate["plan_sha256"] = _sha(plan.read_text(encoding="utf-8"))
    (plan.parent / "draft-content-gate.json").write_text(
        json.dumps(gate), encoding="utf-8"
    )

    assert _draft_apply_recovery_args(plan.parent) == ["--plan", str(plan.resolve())]
