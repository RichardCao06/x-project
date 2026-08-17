"""Regression oracle for the first mixed-state Wiki isolation cohort."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from lca_project.domains.wiki import WikiAdapter
from lca_project.domains.wiki_workspace import WikiWorkspaceBuilder


ROOT = Path(__file__).resolve().parents[2]
EXPECTED = json.loads((ROOT / "defects/wiki/isolation-replay.json").read_text(encoding="utf-8"))


def test_isolation_replay_fixture_keeps_mixed_state_route_and_count_oracle() -> None:
    """Protect A017/P031 audit and P003 rebuild classification from silent drift."""
    assert EXPECTED["nodes"]["A017"]["recommended_mode"] == "audit"
    assert EXPECTED["nodes"]["P031"]["prepare"]["repair_claims_full"] == 31
    assert EXPECTED["nodes"]["P003"]["recommended_mode"] == "rebuild"
    assert EXPECTED["validated"] is True


def test_isolated_mixed_cohort_runs_plan_prepare_validate_without_source_tree(tmp_path: Path) -> None:
    """E2E contract: target adapter must execute its frozen Wiki controller alone.

    This intentionally starts from an empty disposable workspace: no test may
    execute or mutate the original project as an implicit runtime dependency.
    """
    workspace = tmp_path / "isolated"
    built = WikiWorkspaceBuilder().build(workspace)
    WikiWorkspaceBuilder().verify(built.root)
    adapter = WikiAdapter()
    oil = adapter.plan("oil_refining", ["A017"], workspace=workspace, output=workspace / "batches" / "oil", dry_run=False)
    ict = adapter.plan("ict_equipment", ["P031", "P003"], workspace=workspace, output=workspace / "batches" / "ict", dry_run=False)
    assert oil.returncode == 0, oil.stderr
    assert ict.returncode == 0, ict.stderr
    oil_manifest = json.loads((workspace / "batches" / "oil" / "manifest.json").read_text(encoding="utf-8"))
    ict_manifest = json.loads((workspace / "batches" / "ict" / "manifest.json").read_text(encoding="utf-8"))
    assert [(node["node_id"], node["recommended_mode"]) for node in oil_manifest["nodes"]] == [("A017", "audit")]
    assert {(node["node_id"], node["recommended_mode"]) for node in ict_manifest["nodes"]} == {
        ("P031", "audit"), ("P003", "rebuild")}
    for batch in (workspace / "batches" / "oil", workspace / "batches" / "ict"):
        prepared = adapter.command("prepare", batch / "manifest.json", workspace=workspace, dry_run=False)
        assert prepared.returncode == 0, prepared.stderr
        validated = adapter.command("validate", batch / "prepared.json", workspace=workspace, dry_run=False)
        assert validated.returncode == 0, validated.stderr
        assert '"verdict": "PASS"' in validated.stdout
    oil_prepared = json.loads((workspace / "batches" / "oil" / "prepared.json").read_text(encoding="utf-8"))
    ict_prepared = json.loads((workspace / "batches" / "ict" / "prepared.json").read_text(encoding="utf-8"))
    assert oil_prepared["full_claim_counts"] == {"A017": 39}
    assert ict_prepared["sampled_claim_counts"]["P031"] == 31
    assert any(item.get("mode") == "nomination" for item in ict_prepared["workflows"])


def test_workspace_refresh_repairs_incomplete_managed_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "refresh"
    builder = WikiWorkspaceBuilder()
    builder.build(workspace)
    profile = workspace / "profiles/wiki-node-production-profile-v1.json"
    manifest = workspace / "workspace-manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    profile.unlink()
    document["files"] = [row for row in document["files"] if row["path"] != profile.relative_to(workspace).as_posix()]
    manifest.write_text(json.dumps(document), encoding="utf-8")
    generated = workspace / "runs/keep.json"
    generated.parent.mkdir(parents=True)
    generated.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest tree drift"):
        builder.verify(workspace)
    builder.refresh(workspace)
    assert profile.is_file()
    assert generated.is_file()


def test_workspace_refresh_preserves_task_owned_page_and_registry(tmp_path: Path) -> None:
    workspace = tmp_path / "mutable-refresh"
    builder = WikiWorkspaceBuilder()
    builder.build(workspace)
    page = next((workspace / "wiki/ict_equipment/activities").glob("A039--*.md"))
    registry = workspace / "sources/ict_equipment/registry.json"
    page.write_text(page.read_text(encoding="utf-8") + "\n<!-- task-owned -->\n",
                    encoding="utf-8")
    registry.write_text(registry.read_text(encoding="utf-8") + "\n",
                        encoding="utf-8")
    page_before, registry_before = page.read_bytes(), registry.read_bytes()

    builder.refresh(workspace)

    assert page.read_bytes() == page_before
    assert registry.read_bytes() == registry_before
    builder.verify(workspace)


def test_workspace_selective_refresh_only_projects_changed_vendor_inputs(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    shutil.copytree(ROOT / "vendor/lca_cornerstone", vendor)
    workspace = tmp_path / "selective-refresh"
    builder = WikiWorkspaceBuilder(vendor)
    builder.build(workspace)
    selected = vendor / "scripts/wiki_batch.py"
    untouched = vendor / "scripts/wiki_table_population.py"
    selected.write_text(selected.read_text(encoding="utf-8") + "\n# selected\n",
                        encoding="utf-8")
    untouched.write_text(untouched.read_text(encoding="utf-8") + "\n# not selected\n",
                         encoding="utf-8")

    builder.refresh(
        workspace,
        vendor_paths=["vendor/lca_cornerstone/scripts/wiki_batch.py"],
    )

    assert (workspace / "scripts/wiki_batch.py").read_bytes() == selected.read_bytes()
    assert (workspace / "scripts/wiki_table_population.py").read_bytes() != untouched.read_bytes()


def test_production_viewer_is_built_with_the_bundle_in_an_isolated_workspace(tmp_path: Path) -> None:
    """A release is not viewable when only the JS bundle exists."""
    workspace = tmp_path / "viewer"
    WikiWorkspaceBuilder().build(workspace)
    subprocess.run([
        sys.executable, "scripts/build_wiki_bundle.py", "wiki/ict_equipment",
        "docs/ict_equipment-wiki-data.js", "ICT_EQUIPMENT_WIKI",
    ], cwd=workspace, check=True, capture_output=True, text=True)
    subprocess.run([
        sys.executable, "scripts/build_wiki_viewer.py", "ict_equipment",
    ], cwd=workspace, check=True, capture_output=True, text=True)
    viewer = (workspace / "docs/ict_equipment-wiki.html").read_text(encoding="utf-8")
    assert "ict_equipment-wiki-data.js" in viewer
    assert "window.ICT_EQUIPMENT_WIKI" in viewer
    assert "信息与通信技术设备节点 Wiki" in viewer
    subprocess.run([
        sys.executable, "scripts/build_wiki_viewer.py", "ict_equipment",
        "--start-node", "P003",
    ], cwd=workspace, check=True, capture_output=True, text=True)
    node_viewer = (workspace / "docs/ict_equipment-wiki-P003.html").read_text(encoding="utf-8")
    assert 'const start = qp.get(\'id\') || "P003";' in node_viewer
    assert "const defaultNode = start;" in node_viewer
    assert "get('id')||defaultNode" in node_viewer
    assert "const key=s.url?`url:${s.url}`:`id:${s.id}`;" in node_viewer
    assert 'data-source-count="${count}"' in node_viewer
    assert 'id="source-${esc(r.id)}"' in node_viewer
    assert "ict_equipment-wiki-data.js" in node_viewer
