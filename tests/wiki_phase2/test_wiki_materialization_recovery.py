"""Hash-bound recovery for task-owned Wiki materializations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


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
