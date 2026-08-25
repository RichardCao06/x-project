from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

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


def _write_provider_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    plan = tmp_path / "research-plan.json"
    plan.write_text(json.dumps({
        "protocol": "wiki-research-plan-v1", "node_id": "A019",
        "advisory_candidates": [],
    }), encoding="utf-8")
    scout = tmp_path / "research-scout-diversity-repair.json"
    failed_url = "https://failed.example/evidence"
    scout.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1", "node_id": "A019",
        "candidates": [{
            "title": "Novel repair source", "url": failed_url,
            "question_id": "identity.activity_definition", "repair_novel": False,
        }, {
            "title": "Novel repair source", "url": "https://novel.example/evidence",
            "question_id": "identity.activity_definition", "repair_novel": True,
        }],
        "diversity_repair": {
            "failed_question_ids": ["identity.activity_definition"],
            "excluded_urls": [failed_url],
            "excluded_url_hashes": [hashlib.sha256(failed_url.encode()).hexdigest()],
        },
    }), encoding="utf-8")
    # A base scout next to the plan is a trap: provider execution must never
    # silently reconstruct this path when a repair scout was selected.
    (tmp_path / "research-scout.json").write_text(json.dumps({
        "protocol": "wiki-research-scout-v1", "node_id": "A019",
        "candidates": [{
            "title": "Novel repair source", "url": "https://stale.example/evidence",
        }],
    }), encoding="utf-8")
    record = {
        "path": str(scout.resolve()),
        "sha256": hashlib.sha256(scout.read_bytes()).hexdigest(),
    }
    queue = tmp_path / "source-queue.json"
    queue.write_text(json.dumps({
        "research_plan": {
            "path": str(plan.resolve()),
            "sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        },
        "research_scout": record,
        "queries": [{
            "search_hash": "search-1", "query": "repair evidence",
            "research_tracks": [],
            "claim": {"believed_source": "Novel repair source"},
        }],
    }), encoding="utf-8")
    config = tmp_path / "search-providers.json"
    config.write_text(json.dumps({
        "providers": {}, "routing": {}, "query_policy": {},
    }), encoding="utf-8")
    return queue, config, scout, tmp_path / "frozen-provider-search-results.json"


def test_provider_consumes_only_hash_bound_active_repair_scout(tmp_path: Path) -> None:
    queue, config, scout, output = _write_provider_fixture(tmp_path)
    scout_hash = hashlib.sha256(scout.read_bytes()).hexdigest()
    completed = subprocess.run([
        sys.executable, str(ROOT / "scripts/search_provider_runtime.py"),
        str(queue), str(config), str(output),
        "--research-scout", str(scout),
        "--research-scout-sha256", scout_hash,
    ], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    frozen = json.loads(output.read_text(encoding="utf-8"))
    assert frozen["research_scout"] == {
        "path": str(scout.resolve()), "sha256": scout_hash,
    }
    assert frozen["queries"][0]["results"][0]["url"] == (
        "https://novel.example/evidence"
    )
    assert "https://stale.example/evidence" not in json.dumps(frozen)
    assert "https://failed.example/evidence" not in json.dumps(frozen["queries"])


def test_provider_rejects_missing_or_mismatched_active_scout_hash(tmp_path: Path) -> None:
    queue, config, scout, output = _write_provider_fixture(tmp_path)
    missing = subprocess.run([
        sys.executable, str(ROOT / "scripts/search_provider_runtime.py"),
        str(queue), str(config), str(output),
    ], capture_output=True, text=True, check=False)
    mismatch = subprocess.run([
        sys.executable, str(ROOT / "scripts/search_provider_runtime.py"),
        str(queue), str(config), str(output),
        "--research-scout", str(scout), "--research-scout-sha256", "0" * 64,
    ], capture_output=True, text=True, check=False)

    assert missing.returncode != 0
    assert mismatch.returncode != 0
    assert "SHA-256 mismatch" in mismatch.stderr
