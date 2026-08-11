from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lca_project.cli import validate_project
from lca_project.domains.base import AdapterError
from lca_project.domains.bom import BomAdapter
from lca_project.domains.cross_link import CrossLinkAdapter
from lca_project.domains.graph import GraphAdapter
from lca_project.domains.wiki import WikiAdapter
from lca_project.kernel.registry import CapabilityRegistry
from lca_project.kernel.workflow import WorkflowSpec, compile_workflow


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("/Users/shujudagongren/Myspace/lca-cornerstone")


def test_project_configuration_compiles_every_workflow() -> None:
    registry = CapabilityRegistry.load_directory(ROOT / "capabilities")
    compiled = [compile_workflow(WorkflowSpec.from_mapping(json.loads(path.read_text())),
                                 {item.id for item in registry.all()})
                for path in (ROOT / "workflows").glob("*.json")]
    assert len(compiled) == 6 and all(item.order for item in compiled)


def test_migration_hashes_match_source_and_vendored_assets() -> None:
    manifest = json.loads((ROOT / "docs" / "migration-manifest.json").read_text())
    assert manifest["rules"] and manifest["assets"]
    for asset in manifest["assets"]:
        source = Path(asset["source_path"])
        target = ROOT / asset["target_path"]
        assert target.is_file(), target
        target_hash = asset.get("target_sha256", asset["source_sha256"])
        assert hashlib.sha256(target.read_bytes()).hexdigest() == target_hash
        # Local migration runs additionally prove byte identity with the source;
        # CI remains replayable after the old repository is detached.
        if source.is_file():
            assert hashlib.sha256(source.read_bytes()).hexdigest() == asset["source_sha256"]


def test_domain_adapters_reject_missing_external_inputs_without_source_write(tmp_path: Path) -> None:
    source_probe = SOURCE / "scripts" / "validate_graph.py"
    before = hashlib.sha256(source_probe.read_bytes()).hexdigest() if source_probe.is_file() else None
    with pytest.raises(AdapterError):
        GraphAdapter().validate(tmp_path / "missing-graph.json", workspace=tmp_path)
    with pytest.raises(AdapterError):
        BomAdapter().prepare(tmp_path / "missing.xlsx", "auto", workspace=tmp_path)
    if before is not None:
        assert hashlib.sha256(source_probe.read_bytes()).hexdigest() == before


def test_grf_001_graph_gate_is_declared_as_a_capability() -> None:
    raw = json.loads((ROOT / "capabilities" / "graph.gate@1.json").read_text())
    assert raw["side_effects"] == "staged_apply"
    assert raw["entrypoint"] == "vendor/lca_cornerstone/scripts/gate.py"


def test_wiki_003_adapter_only_accepts_local_manifest_files(tmp_path: Path) -> None:
    with pytest.raises(AdapterError):
        WikiAdapter().command("gate", tmp_path / "absent.json", workspace=tmp_path)


def test_wiki_004_empty_node_batch_is_rejected_before_vendor_execution(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="at least one node"):
        WikiAdapter().plan("auto", [], workspace=tmp_path, output="plan.json")


def test_xlc_001_and_bom_002_adapter_input_boundaries_are_explicit(tmp_path: Path) -> None:
    with pytest.raises(AdapterError):
        CrossLinkAdapter().apply(tmp_path / "no-nominations.json", workspace=tmp_path)
    with pytest.raises(AdapterError):
        BomAdapter().grade(tmp_path / "no-matches.json", "vehicle", tmp_path / "no-buckets.json", workspace=tmp_path)


def test_agt_001_to_003_agent_policy_is_deny_by_default() -> None:
    governance = json.loads((ROOT / "policies" / "governance-v1.json").read_text())
    agents = governance["agents"]
    assert agents == {"may_direct_apply": False, "may_self_approve": False, "network_default": "deny"}
    for name in ("researcher", "reviewer", "repairer"):
        definition = json.loads((ROOT / "agents" / name / "agent.json").read_text())
        assert definition["permissions"] == ["artifact:read"]
        assert definition["model"] and definition["reasoning_effort"]
        assert definition["network"] == "deny"


def test_e2e_001_project_validate_and_source_assets_are_read_only() -> None:
    report = validate_project(ROOT)
    assert report["status"] == "pass"
    assert report["migrated_assets"] >= 30
    assert not any((ROOT / name).exists() for name in ("registry", "wiki", "sources"))
