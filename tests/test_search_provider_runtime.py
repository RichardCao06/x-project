from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from lca_project import capability_runtime

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("search_provider_runtime", ROOT / "scripts/search_provider_runtime.py")
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def test_normalize_hits_deduplicates_and_preserves_provider_locator() -> None:
    rows = [{"url": "https://example.com/a", "title": "A", "description": "one"},
            {"url": "https://example.com/a", "title": "duplicate"},
            {"url": "javascript:bad", "title": "bad"}]
    hits = module().normalize_hits(rows, provider="exa", locator="solder dross", limit=10)
    assert hits == [{"url": "https://example.com/a", "title": "A", "snippet": "one",
                     "provider": "exa", "locator": "solder dross"}]


def test_secret_loader_does_not_override_process_environment(tmp_path: Path, monkeypatch) -> None:
    secret_file = tmp_path / ".env"
    secret_file.write_text("EXA_API_KEY=file-value\n", encoding="utf-8")
    monkeypatch.setenv("EXA_API_KEY", "environment-value")
    assert module().load_secrets(secret_file)["EXA_API_KEY"] == "environment-value"


def provider_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, str]:
    excluded_url = "https://excluded.example/stale"
    plan = tmp_path / "research-plan.json"
    plan.write_text(json.dumps({
        "protocol": "wiki-research-plan-v1", "node_id": "A015",
        "advisory_candidates": [{"title": "Stale source", "url": excluded_url}],
    }), encoding="utf-8")
    scout = tmp_path / "research-scout-diversity-repair.json"
    scout.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1", "node_id": "A015",
        "candidates": [{"title": "Current source", "url": "https://current.example/source"}],
        "diversity_repair": {
            "protocol": "wiki-source-diversity-repair-v1", "excluded_urls": [excluded_url],
        },
    }), encoding="utf-8")
    queue = tmp_path / "source-queue.json"
    queue.write_text(json.dumps({
        "research_plan": {"path": str(plan), "sha256": hashlib.sha256(plan.read_bytes()).hexdigest()},
        "queries": [
            {"search_hash": "current", "query": "current", "research_tracks": [],
             "claim": {"believed_source": "Current source"}},
            {"search_hash": "stale", "query": "stale", "research_tracks": [],
             "claim": {"believed_source": "Stale source"}},
        ],
    }), encoding="utf-8")
    config = tmp_path / "search-providers.json"
    config.write_text(json.dumps({"providers": {}, "routing": {}, "query_policy": {}}), encoding="utf-8")
    return queue, config, scout, tmp_path / "results.json", excluded_url


def test_provider_binds_exact_active_repair_scout_and_never_reintroduces_exclusions(
    tmp_path: Path, monkeypatch,
) -> None:
    queue, config, scout, output, excluded_url = provider_inputs(tmp_path)
    digest = hashlib.sha256(scout.read_bytes()).hexdigest()
    monkeypatch.setattr(sys, "argv", [
        "search_provider_runtime.py", str(queue), str(config), str(output),
        "--research-scout", str(scout), "--research-scout-sha256", digest,
    ])

    assert module().main() == 0

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["active_research_scout"] == {"path": str(scout.resolve()), "sha256": digest}
    urls = {hit["url"] for row in result["queries"] for hit in row["results"]}
    assert urls == {"https://current.example/source"}
    assert excluded_url not in urls


def test_provider_rejects_active_scout_hash_mismatch(tmp_path: Path, monkeypatch) -> None:
    queue, config, scout, output, _ = provider_inputs(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "search_provider_runtime.py", str(queue), str(config), str(output),
        "--research-scout", str(scout), "--research-scout-sha256", "0" * 64,
    ])

    with pytest.raises(ValueError, match="active research scout hash mismatch"):
        module().main()
    assert not output.exists()


def test_research_ready_passes_active_scout_path_and_hash_to_provider(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    batch = workspace / "batch"
    batch.mkdir(parents=True)
    (batch / "prepared.json").write_text("{}", encoding="utf-8")
    (batch / "nomination.workflow.run.js").write_text("// frozen", encoding="utf-8")
    plan = batch / "research-plan.json"
    plan.write_text(json.dumps({"protocol": "wiki-research-plan-v1"}), encoding="utf-8")
    scout = batch / "research-scout.json"
    scout.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1", "node_id": "A015",
        "query_policy_version": "activity-process-focus-v2", "candidates": [],
    }), encoding="utf-8")
    calls: list[list[list[str]]] = []

    def successful_pipeline(commands, **kwargs):
        calls.append(commands)
        return {"status": "ok", "steps": []}

    monkeypatch.setattr(capability_runtime, "_pipeline", successful_pipeline)

    result = capability_runtime.agent({
        "phase": "research_ready", "workspace": str(workspace), "batch": str(batch),
        "research_plan": str(plan), "allowed_domains": ["example.com"],
    })

    assert result["status"] == "ok"
    provider_command = calls[1][0]
    scout_index = provider_command.index("--research-scout")
    hash_index = provider_command.index("--research-scout-sha256")
    assert provider_command[scout_index + 1] == str(scout)
    assert provider_command[hash_index + 1] == hashlib.sha256(scout.read_bytes()).hexdigest()
